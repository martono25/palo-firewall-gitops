#!/usr/bin/env bash
# Preflight checks for an AWS account move — the prose prerequisites, executed.
#
# WHY THIS EXISTS. Three account moves (2026-09-14, 2026-10-10, 2026-11-15) went
# wrong in five places, and EVERY ONE was a thing written down rather than
# checked:
#
#   1. "Delete the firewall from CSP first" lived in docs/changing-aws-account.md
#      while rebootstrap-account.sh's step 2 merely ASSUMED it. Nothing was
#      skipped, the dead device's root was imported, and the operator spotted the
#      divergence before the script did.
#   2. "Subscribe to the VM-Series BYOL listing" — unchecked until `terraform
#      apply` failed with OptInRequired.
#   3. "The key pair must match" — a key pair of the right NAME existed in the
#      new account with a DIFFERENT key, and SSH's "Permission denied
#      (publickey)" read as a broken firewall for ten minutes (2026-10-10).
#   4. "Type yes, don't pipe it" — `yes | ./rebootstrap-account.sh` silently
#      cancels every apply, because `yes` prints "y" and terraform wants "yes".
#   5. "The advanced routing engine must be on" — written down NOWHERE until
#      2026-11-16, when a firewall came up addressed, zoned, carrying every rule,
#      and routing nothing, with every check reporting health.
#
# THE SPLIT IS DELIBERATE. `pf_*` functions take VALUES and return a verdict;
# they touch nothing and are tested directly. `check_*` wrappers fetch the real
# values (aws, gh, ssh, openssl) and call them. Logic that cannot be tested
# without a cloud account is logic that gets tested by an outage.
#
# NAMING IS LOAD-BEARING: `pf_*` is a CHECK (pure, tested one by one), `pfx_*` is
# machinery (reporting, counting). A test asserts that every pf_ in this file is
# exercised by name, so a check nobody calls cannot hide among the plumbing.
#
# VERDICTS. Each pf_ function echoes one line and returns:
#   0 = PASS       nothing to do
#   1 = FAIL       blocking; the move must stop
#   2 = WARN       proceed, but the operator should know
# WARN exists because some facts are advisory (temporary credentials will expire
# mid-move) and turning them into blockers teaches people to skip preflight.

# ── pure checks ─────────────────────────────────────────────────────────────

# pf_key_fingerprint <aws fingerprint> <local fingerprint>
#
# AWS shows, for a key pair IT generated, the SHA-1 of the PKCS#8 private key —
# which is why the local side is computed with
#   openssl pkcs8 -topk8 -nocrypt | openssl sha1 -c
# An IMPORTED key pair is fingerprinted differently (MD5 of the public key), so
# a mismatch here is reported as UNCOMPARABLE rather than wrong: refusing a
# launch over a fingerprint we cannot compute the same way would be a false
# alarm, and this check exists to kill a false SSH diagnosis, not to add one.
pf_key_fingerprint() {
  local aws_fp="$1" local_fp="$2"
  if [ -z "$aws_fp" ]; then
    echo "no key pair of that name in this account — create it before launching"
    return 1
  fi
  if [ -z "$local_fp" ]; then
    echo "key pair exists in AWS but no local .pem to compare — SSH will fail with 'Permission denied (publickey)'"
    return 1
  fi
  if [ "$aws_fp" = "$local_fp" ]; then
    echo "key pair matches the local .pem"
    return 0
  fi
  case "$aws_fp" in
    *:*:*:*:*:*:*:*:*:*:*:*:*:*:*:*:*:*:*:*)
      echo "KEY MISMATCH: AWS has ${aws_fp:0:17}…, the local .pem is ${local_fp:0:17}… — launching now gives a firewall you cannot SSH to"
      return 1 ;;
    *)
      echo "key pair fingerprint is not in the AWS-generated form (an imported key?) — cannot compare, verify by hand"
      return 2 ;;
  esac
}

# pf_quota <quota value> <vcpus the instance needs>
pf_quota() {
  local have="$1" need="$2"
  case "$have" in ''|*[!0-9.]*) echo "could not read the vCPU quota — check Service Quotas by hand"; return 2 ;; esac
  if awk -v h="$have" -v n="$need" 'BEGIN{exit !(h+0 < n+0)}'; then
    echo "vCPU quota is $have but the firewall needs $need — the launch will fail"
    return 1
  fi
  echo "vCPU quota $have covers the $need the firewall needs"
  return 0
}

# pf_marketplace <text of a run-instances --dry-run attempt>
#
# A dry run is the only honest check: the subscription is per account, and the
# AMI being VISIBLE says nothing about it. "DryRunOperation" IS the pass.
pf_marketplace() {
  case "$1" in
    *DryRunOperation*)  echo "Marketplace subscription is active (dry run would have succeeded)"; return 0 ;;
    *OptInRequired*)    echo "NOT SUBSCRIBED to the VM-Series listing — subscribe in Marketplace, then re-run"; return 1 ;;
    *UnauthorizedOperation*) echo "credentials cannot describe/launch instances — wrong account or role?"; return 1 ;;
    *)                  echo "could not determine the subscription state: ${1:0:90}"; return 2 ;;
  esac
}

# pf_stdin_is_usable <1 if stdin is a tty> <1 if this run will need an apply>
#
# `yes | ./rebootstrap-account.sh` cancels every apply: `yes` prints "y" and
# terraform accepts only the literal "yes". Two runs were spent on that.
pf_stdin_is_usable() {
  local tty="$1" needs_apply="$2"
  if [ "$needs_apply" != "1" ]; then
    echo "no apply in this run — stdin does not matter"
    return 0
  fi
  if [ "$tty" = "1" ]; then
    echo "stdin is a terminal — terraform can ask for approval"
    return 0
  fi
  echo "not a terminal — rebootstrap-account.sh's applies would read EOF and CANCEL. Run that interactively (\`yes\` prints 'y', not 'yes'; pipe \`yes yes\` only if you must)"
  return 1
}

# pf_credentials_lifetime <access key id>
#
# ASIA… are temporary (SSO/STS) and expire in hours; AKIA… are long-lived. A
# move takes long enough that an expiry mid-run is normal, and it looks like
# everything is broken at once.
pf_credentials_lifetime() {
  case "$1" in
    ASIA*) echo "temporary credentials (SSO/STS) — they expire in hours; re-authenticate before a long run"; return 2 ;;
    AKIA*) echo "long-lived access key — no mid-run expiry, but rotate it when the account is retired"; return 0 ;;
    "")    echo "could not read the access key id"; return 2 ;;
    *)     echo "unrecognised credential type ${1:0:4}…"; return 2 ;;
  esac
}

# pf_declared_firewalls <declared serials, space separated> <registered serials>
#
# The CSP-deletion prerequisite, executed. A serial the catalog declares that
# SCM no longer has is a RETIREMENT (its evidence needs a `Removes:` trailer on
# its own ticket); the reverse is an ADOPTION. Either way the move cannot
# proceed cleanly, and saying which is which is the whole point.
pf_declared_firewalls() {
  local declared="$1" registered="$2" s hit missing="" extra=""
  for s in $declared; do
    hit=0; for r in $registered; do [ "$s" = "$r" ] && hit=1; done
    [ "$hit" = "1" ] || missing="$missing $s"
  done
  for r in $registered; do
    hit=0; for s in $declared; do [ "$s" = "$r" ] && hit=1; done
    [ "$hit" = "1" ] || extra="$extra $r"
  done
  if [ -n "$missing" ]; then
    echo "declared but ABSENT from SCM:${missing} — delete it from CSP if you have not, then retire it IN THE SAME PR as the imports (\`Removes:\` on a NEW ticket)"
    return 1
  fi
  if [ -n "$extra" ]; then
    echo "registered in SCM but NOT declared:${extra} — adopt it (\`fwgitops adopt-device <serial> --folder <f> --ticket <T>\`)"
    return 1
  fi
  if [ -z "$declared$registered" ]; then
    echo "no firewall declared and none registered — nothing to reconcile"
    return 0
  fi
  echo "declared firewalls match SCM"
  return 0
}

# pf_advanced_routing <output of `show advanced-routing logical-router`>
#
# THE SILENT ONE. `scm_logical_router` is an advanced-routing object; with the
# engine off the device accepts SCM's push and ignores it — interfaces addressed,
# forwarding N/A, no routes, every pipeline check green, `device-sync` reporting
# the firewall CURRENT because the config really did arrive. Palo Alto says SCM's
# first push enables the engine and reboots; it did for 007955000919340 and
# 007955000935486 and NOT for 007955000959850 (2026-11-16).
#
# THE PROBE IS THE ROUTER COMMAND, NOT THE SETTING. `show system setting
# advanced-routing` prints `advanced-routing: off` while the engine is off and
# then answers `Invalid syntax` once it is ON — so keying on it reports "could
# not check" for exactly the healthy firewall it was meant to bless. Measured
# both ways on 959850, before and after the reboot. The router command
# discriminates in both directions, which is the only property that matters:
#
#   OFF -> "Server error : show -> advanced-routing is invalid.
#           advanced routing mode is not enabled"
#   ON  -> "ID   NAMESPACE   LOGICAL-ROUTER" (the table header, even when empty)
pf_advanced_routing() {
  case "$1" in
    *"advanced routing mode is not enabled"*|*"advanced-routing: off"*)
      echo "advanced routing engine is OFF: SCM's logical router will be IGNORED (interfaces up, no routes, nothing reports it). Fix: set deviceconfig setting advance-routing yes (NO 'd'), commit, reboot"
      return 1 ;;
    *LOGICAL-ROUTER*|*"advanced-routing: on"*)
      echo "advanced routing engine is ON — logical routers will apply"
      return 0 ;;
    "") echo "no output from the device — could not check advanced routing"; return 2 ;;
    *)  echo "unexpected output: ${1:0:80}"; return 2 ;;
  esac
}

# ── wrappers: fetch the real values, then call the pure check above ─────────
#
# Thin ON PURPOSE. Everything these do is read a value and pass it on, so the
# judgement stays in the tested half and these cannot be where a verdict is
# quietly wrong.

PF_REGION="${PF_REGION:-ap-southeast-1}"
PF_PASS=0 PF_WARN=0 PF_FAIL=0

# pfx_report <name> <verdict rc> <message>
pfx_report() {
  case "$2" in
    0) printf '   \033[32mPASS\033[0m  %-22s %s\n' "$1" "$3"; PF_PASS=$((PF_PASS+1)) ;;
    2) printf '   \033[33mWARN\033[0m  %-22s %s\n' "$1" "$3"; PF_WARN=$((PF_WARN+1)) ;;
    *) printf '   \033[31mFAIL\033[0m  %-22s %s\n' "$1" "$3"; PF_FAIL=$((PF_FAIL+1)) ;;
  esac
}

# pfx_run <name> <pf function> <args...>
pfx_run() {
  local name="$1" fn="$2"; shift 2
  local msg rc=0
  # `|| rc=$?`, not `msg=$(...); rc=$?`. A caller running under `set -e` — which
  # rebootstrap-account.sh does — exits on the assignment the moment a check
  # returns non-zero, so the FIRST failing check killed the run before it could
  # be reported. Measured 2026-11-16: the rehearsal exited 1 printing nothing
  # after "terraform 1.15.8". In an OR list the failure is handled, so `set -e`
  # stays out of it.
  msg=$("$fn" "$@") || rc=$?
  pfx_report "$name" "$rc" "$msg"
}

check_credentials() {
  local id
  id=$(aws configure get aws_access_key_id 2>/dev/null) || true
  [ -n "$id" ] || id="${AWS_ACCESS_KEY_ID:-}"
  pfx_run "credentials" pf_credentials_lifetime "$id"
}

check_stdin() {
  local tty=0 needs_apply="${1:-1}"
  [ -t 0 ] && tty=1
  pfx_run "stdin" pf_stdin_is_usable "$tty" "$needs_apply"
}

# check_key_pair <key name> <path to .pem>
check_key_pair() {
  local name="$1" pem="$2" aws_fp local_fp
  aws_fp=$(aws ec2 describe-key-pairs --region "$PF_REGION" --key-names "$name" \
             --query 'KeyPairs[0].KeyFingerprint' --output text 2>/dev/null) || true
  [ "$aws_fp" = "None" ] && aws_fp=""
  if [ -f "$pem" ]; then
    local_fp=$(openssl pkcs8 -in "$pem" -inform PEM -outform DER -topk8 -nocrypt 2>/dev/null \
                 | openssl sha1 -c 2>/dev/null | awk '{print $NF}') || true
  fi
  pfx_run "key pair" pf_key_fingerprint "$aws_fp" "${local_fp:-}"
}

# check_marketplace <ami id> <instance type>
check_marketplace() {
  local out
  # ALWAYS non-zero: a dry run reports its verdict as an error either way, so
  # `|| true` is not defensive padding here — without it this check ends the run
  # it was added to protect (measured 2026-11-16).
  out=$(aws ec2 run-instances --dry-run --region "$PF_REGION" \
          --image-id "$1" --instance-type "$2" 2>&1) || true
  pfx_run "marketplace" pf_marketplace "$out"
}

# check_quota <instance type>
check_quota() {
  local have need
  have=$(aws service-quotas get-service-quota --region "$PF_REGION" \
           --service-code ec2 --quota-code L-1216C47A --query Quota.Value --output text 2>/dev/null) || true
  need=$(aws ec2 describe-instance-types --region "$PF_REGION" --instance-types "$1" \
           --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null) || true
  pfx_run "vCPU quota" pf_quota "$have" "${need:-4}"
}

# check_firewalls <folders.yaml> — needs SCM credentials in the environment
#
# FAILS CLOSED, and the first live run is why. Both lookups shelled out to
# system `python3`, which has neither `yaml` nor `fwgitops`; both produced
# nothing, two empty strings compared equal, and the check reported "no firewall
# declared and none registered" while the catalog declared 007955000959850 and
# SCM had it registered. A preflight that reports a PASS it did not establish is
# worse than no preflight. The `OK ` sentinel below is what tells "read it,
# found none" from "could not read it" — the distinction the first version lost.
check_firewalls() {
  local py declared registered
  py="$REPO/.venv/bin/python"; [ -x "$py" ] || py=$(command -v python3)
  declared=$("$py" - "$1" <<'DECL' 2>/dev/null
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
out = []
for f in (d.get("folders") or {}).values():
    out += list((f.get("devices") or {}).keys())
print("OK " + " ".join(sorted(out)))
DECL
) || true
  registered=$("$py" - <<'REG' 2>/dev/null
from fwgitops.scmapi import ScmCredentials, ScmSession
s = ScmSession(ScmCredentials.from_env())
d = (s.request("GET", "/config/setup/v1/devices", params={"limit": 200}) or {}).get("data", [])
print("OK " + " ".join(sorted(str(x.get("serial_number")) for x in d if x.get("serial_number"))))
REG
) || true
  case "$declared" in
    OK*) declared="${declared#OK}" ;;
    *) pfx_report "firewalls vs SCM" 1 "could not read $1 — needs PyYAML (pip install -e . in .venv)"; return ;;
  esac
  case "$registered" in
    OK*) registered="${registered#OK}" ;;
    *) pfx_report "firewalls vs SCM" 1 "could not read SCM — needs the fwgitops venv and valid SCM credentials"; return ;;
  esac
  pfx_run "firewalls vs SCM" pf_declared_firewalls "$declared" "$registered"
}

# check_advanced_routing <mgmt ip> <pem> — run on EVERY new firewall
check_advanced_routing() {
  local out
  # `ssh -tt`, NOT `-T`: PAN-OS needs a pty and answers a non-tty session with
  # nothing at all, which this check would then report as "could not check" —
  # the first live run did exactly that. Noted in the operator memory and
  # forgotten here until it bit.
  out=$(printf 'set cli pager off\nshow advanced-routing logical-router\nexit\n' \
        | ssh -tt -i "$2" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
              -o ConnectTimeout=20 "admin@$1" 2>/dev/null \
        | tr -d '\r' | grep -aE "LOGICAL-ROUTER|not enabled|advanced-routing:" | tail -1) || true
  pfx_run "advanced routing" pf_advanced_routing "$out"
}

# pfx_summary — returns 1 if anything blocking was found
pfx_summary() {
  printf '   ── %d pass, %d warn, %d FAIL\n' "$PF_PASS" "$PF_WARN" "$PF_FAIL"
  [ "$PF_FAIL" -eq 0 ]
}
