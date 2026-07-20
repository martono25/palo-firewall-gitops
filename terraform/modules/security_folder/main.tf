# Static module: consumes the compiler's data via for_each. The module itself is
# hand-authored and reviewed once; only the tfvars data changes per request.
#
# for_each keys are the compiler's stable keys (address/service object name, rule
# key), so editing one entry never churns the others (design: Topic-1 rollback).
#
# Verified against PaloAltoNetworks/scm v1.0.11 (spike, 2026-07-19), including a
# live apply against a lab tenant. Findings baked in here:
#   * resources are scm_address / scm_service / scm_security_rule
#   * the tag attribute is `tag` (list(string), singular)
#   * `protocol` is a nested ATTRIBUTE (object), not a block
#   * TAGS MUST PRE-EXIST AS scm_tag OBJECTS — the API validates them as
#     references and rejects free-form strings (INVALID_REFERENCE). Hence the
#     scm_tag resource + dependency below.
#   * the provider cannot handle concurrent token acquisition; apply must run
#     with -parallelism=1 (see .github/workflows/apply.yml)

locals {
  # Every distinct tag used by any object or rule must exist as an scm_tag.
  managed_tags = toset(flatten(concat(
    [for o in values(var.address_objects) : o.tags],
    [for o in values(var.service_objects) : o.tags],
    [for r in values(var.security_rules) : r.tags],
  )))
}

# ── Tag objects (must exist before anything references them) ──────────────
resource "scm_tag" "this" {
  for_each = local.managed_tags

  name     = each.value
  folder   = var.folder
  comments = "Managed by fwgitops"
}

# ── Address objects ───────────────────────────────────────────────────────
resource "scm_address" "this" {
  for_each = var.address_objects

  name   = each.value.name
  folder = each.value.folder # exactly one of folder/snippet/device

  # Exactly one of fqdn / ip_netmask / ip_range / ip_wildcard.
  ip_netmask = each.value.type == "ip-netmask" ? each.value.value : null
  fqdn       = each.value.type == "fqdn" ? each.value.value : null

  tag = each.value.tags # provider attr is `tag`, list(string)

  depends_on = [scm_tag.this]
}

# ── Service objects ───────────────────────────────────────────────────────
resource "scm_service" "this" {
  for_each = var.service_objects

  name   = each.value.name
  folder = each.value.folder

  # `protocol` is a nested attribute (object), NOT a block. Exactly one of
  # tcp/udp must be set; the other is null.
  protocol = {
    tcp = each.value.protocol == "tcp" ? { port = each.value.port } : null
    udp = each.value.protocol == "udp" ? { port = each.value.port } : null
  }

  tag = each.value.tags

  depends_on = [scm_tag.this]
}

# ── Security rules ────────────────────────────────────────────────────────
resource "scm_security_rule" "this" {
  for_each = var.security_rules

  name   = each.value.name
  folder = each.value.folder

  from        = each.value.from_zones
  to          = each.value.to_zones
  source      = each.value.sources
  destination = each.value.destinations
  service     = each.value.services

  # Phase 1 is service/port-based; App-ID is a known Phase-2 gap (docs/DESIGN.md).
  application = ["any"]

  action   = each.value.action
  log_end  = each.value.log_end
  disabled = each.value.disabled
  tag      = each.value.tags

  # Objects (and their tags) before rules — a rule must never reference
  # something that does not yet exist (design: Change Rollback, ordering rule).
  depends_on = [
    scm_tag.this,
    scm_address.this,
    scm_service.this,
  ]
}
