#!/usr/bin/env bash
# Is this repository ready to move to the account you are signed in to?
#
# Run it as often as you like: it READS, and writes nothing anywhere.
#
#   ./terraform/preflight.sh            # the whole move, before you start
#   ./terraform/preflight.sh --launch   # just the firewall-launch prerequisites
#   ./terraform/preflight.sh --device <mgmt-ip>
#                                       # after a build: is it actually routing?
#
# Exit 0 = nothing blocking. Exit 1 = stop and fix what is marked FAIL.
#
# WHY A SCRIPT AND NOT A CHECKLIST. docs/changing-aws-account.md used to carry
# these as prose, and three moves in a row were derailed by a prerequisite
# nobody checked — a firewall still registered in SCM, an unsubscribed
# Marketplace listing, a key pair with the right name and the wrong key, a
# piped `yes`, and an advanced routing engine switched off. Each is now a
# function in terraform/lib/preflight.sh with tests. The doc explains; this
# decides.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
. "$REPO/terraform/lib/preflight.sh"

PILOT=provisioning/aws-vmseries-pilot
TFVARS="$PILOT/terraform.tfvars"
MODE=all DEVICE_IP=""
case "${1:-}" in
  --launch) MODE=launch ;;
  --device) MODE=device; DEVICE_IP="${2:-}"
            [ -n "$DEVICE_IP" ] || { echo "usage: $0 --device <mgmt-ip>" >&2; exit 64; } ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--launch | --device <mgmt-ip>]" >&2; exit 64 ;;
esac

tfvar() { awk -F'"' -v k="^$1" '$0 ~ k {print $2; exit}' "$TFVARS" 2>/dev/null; }

printf '\n\033[1m── preflight\033[0m\n'
ACCT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCT" ]; then
  pfx_report "aws" 1 "not signed in, or the token has expired — \`aws configure\` / SSO login"
  pfx_summary; exit 1
fi
pfx_report "aws" 0 "signed in to $ACCT (state bucket would be fw-gitops-tfstate-$ACCT)"

if [ "$MODE" = device ]; then
  check_advanced_routing "$DEVICE_IP" "$REPO/$(tfvar ssh_key_name 2>/dev/null || true).pem" 2>/dev/null \
    || check_advanced_routing "$DEVICE_IP" "$REPO/fwgitops-pilot.pem"
  pfx_summary; exit $?
fi

# ── always ──────────────────────────────────────────────────────────────────
check_credentials
if gh auth status >/dev/null 2>&1; then
  scopes=$(gh auth status 2>&1 | sed -n 's/.*Token scopes: //p' | head -1)
  case "$scopes" in
    *repo*workflow*|*workflow*repo*) pfx_report "gh" 0 "authenticated, scopes $scopes" ;;
    *) pfx_report "gh" 1 "authenticated but scopes look short ($scopes) — needs repo AND workflow" ;;
  esac
else
  pfx_report "gh" 1 "not authenticated (gh auth login)"
fi

if [ -n "${SCM_CLIENT_ID:-}" ] || [ -f "$HOME/.fwgitops/scm.env" ]; then
  [ -n "${SCM_CLIENT_ID:-}" ] || { set -a; . "$HOME/.fwgitops/scm.env"; set +a; }
  pfx_report "scm creds" 0 "present"
  check_firewalls catalog/folders.yaml
else
  pfx_report "scm creds" 1 "absent — export SCM_CLIENT_ID/SECRET/SCOPE or create ~/.fwgitops/scm.env"
  pfx_report "firewalls vs SCM" 2 "skipped: needs SCM credentials"
fi

# ── the launch prerequisites ────────────────────────────────────────────────
AMI=$(tfvar vmseries_ami_id); ITYPE=$(tfvar instance_type); KEY=$(tfvar ssh_key_name)
if [ -n "$AMI" ] && [ -n "$ITYPE" ]; then
  check_key_pair "${KEY:-fwgitops-pilot}" "$REPO/${KEY:-fwgitops-pilot}.pem"
  check_marketplace "$AMI" "$ITYPE"
  check_quota "$ITYPE"
else
  pfx_report "launch inputs" 2 "could not read $TFVARS — skipping key pair, marketplace and quota"
fi

# `--launch` stops here; the full run also checks what the move itself needs.
if [ "$MODE" = all ]; then
  check_stdin 1
  tfv=$(terraform version -json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null)
  if python3 -c "import sys; v=tuple(int(x) for x in '${tfv:-0.0}'.split('.')[:2]); sys.exit(0 if v>=(1,10) else 1)" 2>/dev/null; then
    pfx_report "terraform" 0 "$tfv (>= 1.10, native S3 locking)"
  else
    pfx_report "terraform" 1 "${tfv:-not found} — 1.10+ required for use_lockfile"
  fi
fi

printf '\n'
if pfx_summary; then
  printf '\033[1m── ready. Next: ./terraform/rebootstrap-account.sh\033[0m\n'
else
  printf '\033[1m── NOT ready: fix every FAIL above first.\033[0m\n'
  exit 1
fi
