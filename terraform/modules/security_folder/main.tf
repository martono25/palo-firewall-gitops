# Static module: consumes the compiler's data via for_each. The module itself is
# hand-authored and reviewed once; only the tfvars data changes per request.
#
# for_each keys are the compiler's stable keys (address/service object name, rule
# key), so editing one entry never churns the others (design: Topic-1 rollback).
#
# Schema verified against PaloAltoNetworks/scm v1.0.11 (Part-A spike, 2026-07-19).
# Resolved: resource names are scm_address / scm_service / scm_security_rule;
# the tag attribute is `tag` (list(string), singular); `protocol` is a nested
# ATTRIBUTE (object), not a block; scope is exactly one of folder/snippet/device.
# Still open (Part B, needs the tenant): whether SCM requires tag strings to
# pre-exist as scm_tag objects, and the commit/push model.

# ── Address objects ───────────────────────────────────────────────────────
resource "scm_address" "this" {
  for_each = var.address_objects

  name   = each.value.name
  folder = each.value.folder # exactly one of folder/snippet/device

  # Exactly one of fqdn / ip_netmask / ip_range / ip_wildcard.
  ip_netmask = each.value.type == "ip-netmask" ? each.value.value : null
  fqdn       = each.value.type == "fqdn" ? each.value.value : null

  tag = each.value.tags # provider attr is `tag`, list(string)
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

  # Objects before rules — a rule must never reference an object that does not
  # yet exist (design: Change Rollback & Cancellation, ordering rule).
  depends_on = [
    scm_address.this,
    scm_service.this,
  ]
}
