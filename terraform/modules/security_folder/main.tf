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

  # Reference each tag resource (not raw strings) so this object depends on ONLY
  # the tags it uses — a fine-grained edge, not a blanket `depends_on`.
  tag = [for t in each.value.tags : scm_tag.this[t].name]
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

  tag = [for t in each.value.tags : scm_tag.this[t].name]
}

# ── Security rules ────────────────────────────────────────────────────────
resource "scm_security_rule" "this" {
  for_each = var.security_rules

  name   = each.value.name
  folder = each.value.folder

  # Reference the scm_zone RESOURCE for zones this module manages, so Terraform
  # orders the zone before the rule that uses it. Baseline zones (`local`,
  # `internet`, `proxy` …) already exist on the device and are NOT
  # Terraform-managed, so they pass through as plain strings — indexing
  # scm_zone.this["local"] would error.
  #
  # The predicate reads var.zones, NOT scm_zone.this: keying off the variable
  # keeps the branch decidable at plan time. Deliberately not a blanket
  # `depends_on` — see the note above on the destroy-cascade that pattern caused
  # for address objects.
  from = [for z in each.value.from_zones : contains(keys(var.zones), z) ? scm_zone.this[z].name : z]
  to   = [for z in each.value.to_zones : contains(keys(var.zones), z) ? scm_zone.this[z].name : z]

  # Reference the object RESOURCES (not raw strings from tfvars) so each rule
  # depends on ONLY the addresses/services/tags it actually uses. This replaces a
  # coarse `depends_on = [scm_address.this, ...]` (which made every rule depend on
  # EVERY object instance) — under which a `terraform destroy -target` of one
  # address cascaded into destroying ALL rules. Fine-grained edges also give
  # correct create ordering (objects before the rule that references them) with
  # no explicit depends_on.
  source      = [for s in each.value.sources : scm_address.this[s].name]
  destination = [for d in each.value.destinations : scm_address.this[d].name]
  service     = [for v in each.value.services : scm_service.this[v].name]
  tag         = [for t in each.value.tags : scm_tag.this[t].name]

  action   = each.value.action
  log_end  = each.value.log_end
  disabled = each.value.disabled

  # `application` is REQUIRED by the SCM API on every write, so it stays here as
  # the ["any"] skeleton default (omitting it makes the provider send an invalid
  # payload — API_I00035). Its REAL App-ID value is set by `fwgitops enrich`; the
  # provider treats application as computed (ignores config for diffing), so it
  # never reverts enrich's value.
  application = each.value.application

  # The rest of the ADR-0003 enrichment — profile_setting / log_setting / ordering
  # — is deliberately NOT wired here. The scm provider (v1.0.11 AND 1.0.12-beta.3)
  # silently DROPS these on security rules (accepts them in config, treats them as
  # computed, never writes them — verified live 2026-07-28). Wiring them only
  # produced churn (e.g. a log_setting -> null clobber diff) with no on-device
  # effect. `fwgitops enrich` sets them via the SCM API post-apply / pre-push (same
  # candidate, so the push commits skeleton + enrichment atomically). See
  # docs/adr/0003 + src/fwgitops/enrich.py.
}

# ── Zones (ZoneRequest, ADR-0001 kind #2) ─────────────────────────────────
# scm_zone has NO `tag` attribute (unlike scm_security_rule), so zones cannot
# carry the `gitops:` provenance markers and are invisible to drift.py's
# tag-based detection. That is a property of the provider, not an oversight —
# only 14 of its resources are taggable. Recorded in ADR-0001.
resource "scm_zone" "this" {
  for_each = var.zones

  name   = each.value.name
  folder = each.value.folder

  network                      = each.value.network
  enable_user_identification   = each.value.enable_user_identification
  enable_device_identification = each.value.enable_device_identification
  dos_profile                  = each.value.dos_profile
  dos_log_setting              = each.value.dos_log_setting
  user_acl                     = each.value.user_acl
  device_acl                   = each.value.device_acl
}

# ── Interfaces (InterfaceRequest, ADR-0001 kind #3) ───────────────────────
# Like scm_zone, scm_ethernet_interface has NO `tag` attribute, so interfaces
# cannot carry `gitops:` provenance and are invisible to tag-based drift. They
# use the state-based engine instead (drift_engine="state" in the registry).
resource "scm_ethernet_interface" "this" {
  for_each = var.interfaces

  name   = each.value.name
  folder = each.value.folder

  comment = each.value.comment
  layer3  = each.value.layer3
}
