#!/usr/bin/env bash
# spike/schema-answers.sh — answer the scm-provider spike checklist from a schema dump.
#
# Part A needs NO SCM credentials — it only downloads the provider and reads its
# schema. Generate the dump from the isolated probe dir, then run this:
#
#   cd spike/schema-probe
#   terraform init
#   terraform providers schema -json > ../schema.json
#   cd ../.. && ./spike/schema-answers.sh spike/schema.json
#
# Output is a readable answer sheet mapping 1:1 to docs/SPIKE-scm.md Part A.
set -euo pipefail

SCHEMA="${1:-spike/schema.json}"
[ -f "$SCHEMA" ] || {
  echo "usage: $0 [schema.json]" >&2
  echo "generate with: terraform providers schema -json > $SCHEMA" >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

PK=$(jq -r '.provider_schemas // {} | keys[]' "$SCHEMA" | grep -i scm | head -1 || true)
[ -n "$PK" ] || { echo "no 'scm' provider found in $SCHEMA" >&2; exit 1; }

q() { jq -r "$1" "$SCHEMA" 2>/dev/null || true; }
attrs()  { q ".provider_schemas[\"$PK\"].resource_schemas[\"$1\"].block.attributes  // {} | keys[]"; }
blocks() { q ".provider_schemas[\"$PK\"].resource_schemas[\"$1\"].block.block_types // {} | keys[]"; }
has()    { printf '%s\n' "$2" | grep -qx "$1" && echo "YES" || echo "no"; }
first_match() { # first resource name matching a regex
  q '.provider_schemas["'"$PK"'"].resource_schemas // {} | keys[]' | grep -Ei "$1" | head -1 || true
}

echo "=================================================================="
echo " scm provider spike — Part A answer sheet"
echo " provider: $PK"
echo "=================================================================="

echo
echo "── Q: provider AUTH attributes (feeds T1) ────────────────────────"
PROV_ATTRS=$(q ".provider_schemas[\"$PK\"].provider.block.attributes // {} | keys[]")
echo "${PROV_ATTRS:-  (none found)}" | sed 's/^/  /'

echo
echo "── All scm_* resources ───────────────────────────────────────────"
ALL=$(q ".provider_schemas[\"$PK\"].resource_schemas // {} | keys[]")
echo "${ALL:-  (none)}" | sed 's/^/  /'

ADDR=$(first_match '^scm_address$|address_object$|^scm_address')
SVC=$(first_match '^scm_service$|service$')
RULE=$(first_match 'security.*rule')
# Prefer the exact scm_tag resource; only fall back to a looser match (avoids
# picking up e.g. scm_link_tag, which is a different thing).
TAG=$(q ".provider_schemas[\"$PK\"].resource_schemas // {} | keys[]" | grep -Ex 'scm_tag' | head -1 || true)
[ -n "$TAG" ] || TAG=$(first_match '_tag$')

echo
echo "── Q: resource NAMES (checklist item 1) ──────────────────────────"
echo "  address : ${ADDR:-NOT FOUND}"
echo "  service : ${SVC:-NOT FOUND}"
echo "  rule    : ${RULE:-NOT FOUND}   <- resolves scm_security_policy_rule vs scm_security_rule"
echo "  tag     : ${TAG:-none}          <- if present, tags may need to be OBJECTS"

if [ -n "$ADDR" ]; then
  A=$(attrs "$ADDR")
  echo
  echo "── Q: ADDRESS attrs — scope + type + tags ────────────────────────"
  echo "$A" | sed 's/^/  /'
  echo "  --"
  echo "  scope: folder=$(has folder "$A")  snippet=$(has snippet "$A")  device=$(has device "$A")"
  echo "  type : ip_netmask=$(has ip_netmask "$A")  fqdn=$(has fqdn "$A")  ip_range=$(has ip_range "$A")"
  echo "  tags : tags=$(has tags "$A")  tag=$(has tag "$A")   <- HIGH IMPACT on fwgitops.tags"
fi

if [ -n "$SVC" ]; then
  echo
  echo "── Q: SERVICE attrs + nested blocks (protocol shape) ─────────────"
  echo "  attributes:"; attrs "$SVC" | sed 's/^/    /'
  echo "  nested blocks:"; blocks "$SVC" | sed 's/^/    /'
  echo "  (expect a protocol block containing tcp/udp with a port field)"
fi

if [ -n "$RULE" ]; then
  R=$(attrs "$RULE")
  echo
  echo "── Q: RULE attrs — members, application, logging ─────────────────"
  echo "$R" | sed 's/^/  /'
  echo "  nested blocks:"; blocks "$RULE" | sed 's/^/    /'
  echo "  --"
  echo "  members: from=$(has from "$R") to=$(has to "$R") source=$(has source "$R") destination=$(has destination "$R") service=$(has service "$R")"
  echo "  app    : application=$(has application "$R")"
  echo "  logging: log_end=$(has log_end "$R")  log_setting=$(has log_setting "$R")  log_start=$(has log_start "$R")"
  echo "  action : action=$(has action "$R")"
fi

echo
echo "=================================================================="
echo " Next: paste this output back. Then we fix every # VERIFY: in"
echo " terraform/modules/security_folder/ and write the Part B smoke test."
echo "=================================================================="
