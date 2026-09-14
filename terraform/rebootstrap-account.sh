#!/usr/bin/env bash
# Re-bootstrap the platform into a NEW AWS account — or rehearse it in this one.
#
# WHY THIS EXISTS. The pilot's AWS account is a time-limited subscription. When
# 162504351755 expired on 2026-08-29 it took the Terraform state bucket and the
# CI OIDC role with it; every nightly drift and remediate run failed for 17 days,
# and the recovery on 2026-09-14 was a day of hand-run steps. This is those
# steps, in order, each one checked.
#
# USAGE (from the repo root, signed in to the target account with `aws`):
#
#   ./terraform/rebootstrap-account.sh --rehearse   # safe, any time: proves the
#                                                   # state rebuild against LIVE
#                                                   # SCM using EMPTY scratch state.
#                                                   # Writes nothing real.
#   ./terraform/rebootstrap-account.sh              # the real move
#
# WHAT IT DOES (real run):
#   1. preflight   account id, gh auth, SCM credentials, terraform >= 1.10
#   2. archive     local state of bootstrap-backend / github-oidc that names a
#                  DIFFERENT account — moved aside, never deleted
#   3. bucket      terraform apply bootstrap-backend  (you type `yes`)
#   4. CI role     terraform apply github-oidc        (you type `yes`)
#                  + gh variable set AWS_OIDC_ROLE_ARN
#   5. backends    backend.hcl for every root and the pilot
#   6. imports     compile, then `fwgitops recover-state`, then a plan per root
#                  that MUST show 0 to add and 0 to destroy — or it stops
#
# WHAT IT DOES NOT DO, deliberately — each needs a decision or a ticket:
#   - merge the import PR, or approve its apply
#   - retire the old firewall (a `Removes:` trailer on a NEW ticket)
#   - launch the VM (provisioning/aws-vmseries-pilot) or adopt its serial
#     (`fwgitops adopt-device <serial> --folder prod-edge --ticket <T>`)
# The NEXT STEPS printed at the end name each one.
#
# It is safe to re-run. Every step detects "already done" and moves on.
set -euo pipefail

REHEARSE=0
case "${1:-}" in
  --rehearse) REHEARSE=1 ;;
  "") ;;
  -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--rehearse]" >&2; exit 64 ;;
esac

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
REGION=ap-southeast-1
FW="${FWGITOPS:-$REPO/.venv/bin/fwgitops}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
[ -x "$FW" ] || FW=fwgitops
[ -x "$PY" ] || PY=python3

step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
ok()   { printf '   ok  %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Folder and device roots: every terraform/*/ that calls the module. The
# run-once AWS roots (local state) and the module itself are not roots here.
scm_roots() {
  for main in terraform/*/main.tf; do
    d=$(dirname "$main"); n=$(basename "$d")
    case "$n" in bootstrap-*|github-oidc|modules) continue ;; esac
    grep -q 'source = "../modules/security_folder"' "$main" && echo "$n"
  done
}

# ── 1. preflight ─────────────────────────────────────────────────────────────
step "1. preflight"
ACCT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || die "aws is not signed in. Run \`aws configure\` (or SSO) for the target account."
BUCKET="fw-gitops-tfstate-${ACCT}"
ok "AWS account $ACCT  (state bucket $BUCKET)"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
ok "gh authenticated"
if [ -z "${SCM_CLIENT_ID:-}" ]; then
  [ -f "$HOME/.fwgitops/scm.env" ] || die "no SCM credentials: export SCM_CLIENT_ID/SECRET/SCOPE or create ~/.fwgitops/scm.env"
  set -a; . "$HOME/.fwgitops/scm.env"; set +a
fi
ok "SCM credentials present"
TFV=$(terraform version -json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])')
"$PY" - "$TFV" <<'PY' || die "terraform >= 1.10 required (native S3 locking), found $TFV"
import sys; v=tuple(int(x) for x in sys.argv[1].split(".")[:2]); sys.exit(0 if v >= (1, 10) else 1)
PY
ok "terraform $TFV"

# ── rehearsal: the state rebuild against LIVE SCM, with EMPTY scratch state ──
if [ "$REHEARSE" = 1 ]; then
  aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
    || die "rehearsal needs this account's state bucket ($BUCKET) to exist — it rehearses the IMPORT, not the bucket"
  step "rehearsal — every root copied into a scratch dir backed by an EMPTY state key"
  "$FW" compile intent >/dev/null
  "$FW" folder-interfaces --out terraform >/dev/null
  STAMP=$(date -u +%Y%m%dT%H%M%S)
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/rebootstrap-rehearsal.XXXXXX")
  PREFIX="rehearsal-${STAMP}"
  cleanup() {
    rm -rf "$WORK"
    aws s3 rm "s3://${BUCKET}/${PREFIX}/" --recursive >/dev/null 2>&1 || true
  }
  trap cleanup EXIT
  mkdir -p "$WORK/terraform"
  ln -s "$REPO/terraform/modules" "$WORK/terraform/modules"
  for n in $(scm_roots); do
    mkdir -p "$WORK/terraform/$n"
    cp terraform/"$n"/*.tf terraform/"$n"/*.auto.tfvars.json "$WORK/terraform/$n/" 2>/dev/null || true
    rm -f "$WORK/terraform/$n/imports_recovery.tf"
    [ -f "terraform/$n/.terraform.lock.hcl" ] && cp "terraform/$n/.terraform.lock.hcl" "$WORK/terraform/$n/"
    printf 'bucket       = "%s"\nregion       = "%s"\nkey          = "%s/%s/terraform.tfstate"\nencrypt      = true\nuse_lockfile = true\n' \
      "$BUCKET" "$REGION" "$PREFIX" "$n" > "$WORK/terraform/$n/backend.hcl"
  done
  "$FW" recover-state --out "$WORK/terraform" || die "recover-state refused — see above"
  FAIL=0
  for n in $(scm_roots); do
    [ -f "$WORK/terraform/$n/imports_recovery.tf" ] || { ok "$n: nothing to import (skipped above)"; continue; }
    terraform -chdir="$WORK/terraform/$n" init -input=false -no-color -backend-config=backend.hcl >/dev/null
    line=$(terraform -chdir="$WORK/terraform/$n" plan -input=false -no-color -parallelism=1 2>&1 \
             | grep -E '^Plan:|^No changes|^Error' | head -1 || true)
    if "$PY" -c 'import sys; from fwgitops.recover import plan_is_safe; sys.exit(0 if plan_is_safe(sys.argv[1] or None) else 1)' "$line"; then
      ok "$n: $line"
    else
      printf '   \033[31mFAIL\033[0m %s: %s\n' "$n" "${line:-no plan line}"; FAIL=1
    fi
  done
  [ "$FAIL" = 0 ] || die "rehearsal FAILED — a real move would create or destroy objects SCM already holds"
  step "rehearsal PASSED — a real move rebuilds state with nothing created and nothing destroyed"
  exit 0
fi

# ── 2. archive local state that belongs to another account ──────────────────
step "2. local state of the run-once AWS roots"
for d in bootstrap-backend github-oidc; do
  f="terraform/$d/terraform.tfstate"
  [ -f "$f" ] || { ok "$d: no local state"; continue; }
  old=$(grep -oE 'arn:aws:iam::[0-9]{12}' "$f" | head -1 | cut -d: -f5 || true)
  if [ -n "$old" ] && [ "$old" != "$ACCT" ]; then
    mkdir -p "terraform/$d/.old-account-$old"
    mv terraform/"$d"/terraform.tfstate* "terraform/$d/.old-account-$old/"
    ok "$d: state from account $old moved to .old-account-$old/ (kept, gitignored)"
  else
    ok "$d: state already belongs to $ACCT"
  fi
done

# ── 3. state bucket ──────────────────────────────────────────────────────────
step "3. state bucket — terraform/bootstrap-backend"
terraform -chdir=terraform/bootstrap-backend init -input=false -no-color >/dev/null
terraform -chdir=terraform/bootstrap-backend apply -input=true -no-color
[ "$(terraform -chdir=terraform/bootstrap-backend output -raw state_bucket)" = "$BUCKET" ] \
  || die "bootstrap-backend output does not name $BUCKET"
aws s3api get-bucket-versioning --bucket "$BUCKET" --query Status --output text | grep -q Enabled \
  || die "$BUCKET exists but versioning is not Enabled"
ok "$BUCKET exists, versioned"

# ── 4. CI role + repo variable ───────────────────────────────────────────────
step "4. CI OIDC role — terraform/github-oidc"
terraform -chdir=terraform/github-oidc init -input=false -no-color >/dev/null
if aws iam get-open-id-connect-provider \
     --open-id-connect-provider-arn "arn:aws:iam::${ACCT}:oidc-provider/token.actions.githubusercontent.com" >/dev/null 2>&1 \
   && ! terraform -chdir=terraform/github-oidc state list 2>/dev/null | grep -q aws_iam_openid_connect_provider.github; then
  # Account-global: a second one cannot be created, so adopt the existing one.
  terraform -chdir=terraform/github-oidc import -input=false -no-color aws_iam_openid_connect_provider.github \
    "arn:aws:iam::${ACCT}:oidc-provider/token.actions.githubusercontent.com" >/dev/null
  ok "imported the account's existing GitHub OIDC provider"
fi
terraform -chdir=terraform/github-oidc apply -input=true -no-color
ROLE=$(terraform -chdir=terraform/github-oidc output -raw ci_role_arn)
case "$ROLE" in arn:aws:iam::"$ACCT":role/*) ;; *) die "ci_role_arn $ROLE is not in account $ACCT" ;; esac
gh variable set AWS_OIDC_ROLE_ARN --body "$ROLE" >/dev/null
[ "$(gh variable get AWS_OIDC_ROLE_ARN)" = "$ROLE" ] || die "AWS_OIDC_ROLE_ARN did not take"
ok "AWS_OIDC_ROLE_ARN = $ROLE"

# ── 5. backend.hcl everywhere ────────────────────────────────────────────────
step "5. backend.hcl for every root and the pilot"
for n in $(scm_roots); do ./terraform/make-backend.sh "$n" >/dev/null; ok "terraform/$n"; done
PILOT=provisioning/aws-vmseries-pilot
if [ -d "$PILOT" ]; then
  printf 'bucket       = "%s"\nregion       = "%s"\nkey          = "aws-vmseries-pilot/terraform.tfstate"\nencrypt      = true\nuse_lockfile = true\n' \
    "$BUCKET" "$REGION" > "$PILOT/backend.hcl"
  ok "$PILOT"
fi

# ── 6. import blocks, and the plan gate ──────────────────────────────────────
step "6. re-attach state to what SCM already holds"
"$FW" compile intent >/dev/null
"$FW" folder-interfaces --out terraform >/dev/null
EMPTY=()
for n in $(scm_roots); do
  terraform -chdir="terraform/$n" init -input=false -no-color -reconfigure -backend-config=backend.hcl >/dev/null
  if [ -n "$(terraform -chdir="terraform/$n" state list 2>/dev/null)" ]; then
    ok "$n: state already populated — not a recovery target"
  else
    EMPTY+=("--root" "$n")
  fi
done
if [ ${#EMPTY[@]} -eq 0 ]; then
  step "nothing to recover — every root already has state"
  exit 0
fi
"$FW" recover-state "${EMPTY[@]}" || die "recover-state refused — nothing written; see above"
FAIL=0
for n in $(scm_roots); do
  [ -f "terraform/$n/imports_recovery.tf" ] || continue
  line=$(terraform -chdir="terraform/$n" plan -input=false -no-color -parallelism=1 2>&1 \
           | grep -E '^Plan:|^No changes|^Error' | head -1 || true)
  if "$PY" -c 'import sys; from fwgitops.recover import plan_is_safe; sys.exit(0 if plan_is_safe(sys.argv[1] or None) else 1)' "$line"; then
    ok "$n: $line"
  else
    printf '   \033[31mFAIL\033[0m %s: %s\n' "$n" "${line:-no plan line}"; FAIL=1
  fi
done
[ "$FAIL" = 0 ] || die "a plan would create or destroy objects SCM already holds. Do NOT merge the import blocks."

# ── next ─────────────────────────────────────────────────────────────────────
step "DONE with the automated part. NEXT, in order:"
cat <<EOF
  1. Commit the import blocks and open a PR (merging triggers apply.yml):
       git checkout -b fix/state-recovery-${ACCT}
       git add terraform/*/imports_recovery.tf && git commit -m "fix(state): re-import after the move to ${ACCT}"
  2. Any SKIPPED device root above is a dead firewall. In the SAME PR, retire it:
       delete its intents, remove it from catalog/folders.yaml + interfaces.yaml,
       delete terraform/device-<serial>/, and put in the PR BODY, on a NEW ticket:
         Removes: REQ-XXXX-XXXX (TICKET)
  3. Merge; approve the apply if it waits at firewall-apply. Then delete the
     imports_recovery.tf files in a follow-up PR.
  4. Launch the replacement VM: cd ${PILOT} && terraform plan -out=p && terraform apply p
  5. Adopt it:  fwgitops adopt-device <serial> --folder prod-edge --ticket <TICKET>
     then new InterfaceRequests addressed to the new ENI IPs.
  6. Dispatch drift-detect and read it: gh workflow run drift-detect.yml
EOF
