#!/usr/bin/env bash
# Does Terraform order "UPDATE the rule to drop a tag" before "DESTROY that tag"?
#
# TODOS asserts it does not, and that the destroy runs first and hits
# 409 NON_ZERO_REFS. That was observed once on 2026-08-05 during a migration; the
# MECHANISM was never confirmed, and on 2026-08-09 a whole-rule removal ordered
# CORRECTLY (rule destroyed, then tag) — because destroying the rule keeps the
# dependency edge that an in-place UPDATE dissolves. So the bug, if real, is
# specific to a tag VALUE change: a corrected ticket number.
#
# FAIL-SAFE: the probe rule is `deny` on TEST-NET-1 (192.0.2.0/24), so even if it
# were somehow committed it matches no real traffic and grants nothing. This
# script NEVER pushes — everything stays in the SCM candidate — and it cleans up.
set -euo pipefail
cd "$(dirname "$0")"
: "${SCM_CLIENT_ID:?source ~/.fwgitops/scm.env first}"
ROOT=$(mktemp -d)
REPO=$(cd ../.. && pwd)
cp "$REPO"/terraform/prod-edge/*.tf "$ROOT"/; rm -f "$ROOT"/backend.tf
sed -i '' "s#\"../modules/security_folder\"#\"$REPO/terraform/modules/security_folder\"#" "$ROOT"/main.tf
cd "$ROOT"
terraform init -input=false -no-color >/dev/null

echo "── PHASE 1: create the rule tagged PROBE-AAAA ────────────────────"
cp "$REPO"/spike/tag-destroy-ordering/phase1-tag-AAAA.json rules.auto.tfvars.json
terraform apply -input=false -auto-approve -parallelism=1 -no-color | tail -3

echo
echo "── PHASE 2: change the tag VALUE to PROBE-BBBB ───────────────────"
echo "   (rule UPDATED in place; old tag object DESTROYED)"
cp "$REPO"/spike/tag-destroy-ordering/phase2-tag-BBBB.json rules.auto.tfvars.json
set +e
terraform apply -input=false -auto-approve -parallelism=1 -no-color 2>&1 | tee /tmp/phase2.txt
rc=${PIPESTATUS[0]}
set -e
echo
if [ "$rc" -ne 0 ] && grep -q "NON_ZERO_REFS" /tmp/phase2.txt; then
  echo "RESULT: REPRODUCED — the tag destroy ran before the rule update (409 NON_ZERO_REFS)"
elif [ "$rc" -eq 0 ]; then
  echo "RESULT: NOT REPRODUCED — Terraform ordered the update before the destroy"
else
  echo "RESULT: FAILED for another reason — read /tmp/phase2.txt before concluding anything"
fi

echo
echo "── CLEANUP ───────────────────────────────────────────────────────"
terraform destroy -input=false -auto-approve -parallelism=1 -no-color | tail -2
echo "scratch root: $ROOT (local state; nothing was pushed)"
