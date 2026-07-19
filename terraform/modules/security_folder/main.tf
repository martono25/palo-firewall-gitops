# Static module: consumes the compiler's data via for_each. The module itself is
# hand-authored and reviewed once; only the tfvars data changes per request.
#
# for_each keys are the compiler's stable keys (address/service object name, rule
# key), so editing one entry never churns the others (design: Topic-1 rollback).
#
# ⚠️  Every `# VERIFY:` line is a schema assumption to confirm against the scm
#     provider docs during the spike. The mapping is intentionally centralized
#     here so corrections are a single-file edit.

# ── Address objects ───────────────────────────────────────────────────────
resource "scm_address_object" "this" {
  for_each = var.address_objects

  name   = each.value.name
  folder = each.value.folder # VERIFY: scope attr — exactly one of folder/snippet/device

  # VERIFY: scm_address_object expresses type via exactly one of these attrs.
  ip_netmask = each.value.type == "ip-netmask" ? each.value.value : null
  fqdn       = each.value.type == "fqdn" ? each.value.value : null

  tags = each.value.tags # VERIFY: attribute name (tag vs tags) and element type
}

# ── Service objects ───────────────────────────────────────────────────────
resource "scm_service" "this" {
  for_each = var.service_objects

  name   = each.value.name
  folder = each.value.folder

  # VERIFY: scm_service protocol shape. Commonly a nested block:
  #   protocol { tcp { port = "443" } }  /  protocol { udp { port = "..." } }
  protocol {
    dynamic "tcp" {
      for_each = each.value.protocol == "tcp" ? [1] : []
      content {
        port = each.value.port
      }
    }
    dynamic "udp" {
      for_each = each.value.protocol == "udp" ? [1] : []
      content {
        port = each.value.port
      }
    }
  }

  tags = each.value.tags
}

# ── Security rules ────────────────────────────────────────────────────────
resource "scm_security_policy_rule" "this" { # VERIFY: resource name (…_policy_rule vs …_rule)
  for_each = var.security_rules

  name   = each.value.name
  folder = each.value.folder

  # VERIFY: attribute names for zones/members (from/to/source/destination/service).
  from        = each.value.from_zones
  to          = each.value.to_zones
  source      = each.value.sources
  destination = each.value.destinations
  service     = each.value.services

  # Phase 1 is service/port-based; App-ID is a known Phase-2 gap (docs/DESIGN.md).
  application = ["any"] # VERIFY: attr name + whether "any" is the correct literal

  action  = each.value.action
  log_end = each.value.log_end # VERIFY: log_end vs log_setting/log_start
  tags    = each.value.tags

  # Objects before rules — a rule must never reference an object that does not
  # yet exist (design: Change Rollback & Cancellation, ordering rule).
  depends_on = [
    scm_address_object.this,
    scm_service.this,
  ]
}
