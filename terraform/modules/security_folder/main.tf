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
  # LITERAL service values — passed through UNRESOLVED, because they name no
  # object. `application-default` is what an application-matched rule (ICMP)
  # carries: scm_service requires a port, so ICMP cannot be a service object at
  # all. MEASURED in spike/icmp-service-shape (2026-08-09): SCM accepts
  # `application-default` and `any`, and REJECTS a rule with no service key at
  # all (400 "service" is required) even though the provider schema marks the
  # attribute optional.
  #
  # Anything NOT in this set is still resolved through scm_service.this, which
  # keeps the fine-grained dependency edge that orders object-before-rule. A
  # literal has no object to depend on, so nothing is lost by skipping it — and
  # a typo'd service name still fails loudly on the lookup rather than being
  # passed through as a literal.
  literal_services = toset(["application-default", "any"])

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
  service = [for v in each.value.services :
  contains(local.literal_services, v) ? v : scm_service.this[v].name]
  tag = [for t in each.value.tags : scm_tag.this[t].name]

  action   = each.value.action
  log_end  = each.value.log_end
  disabled = each.value.disabled

  # `application` is REQUIRED by the SCM API on every write, so it stays here as
  # the ["any"] skeleton default (omitting it makes the provider send an invalid
  # payload — API_I00035). Its REAL App-ID value is set by `fwgitops enrich`; the
  # provider treats application as computed (ignores config for diffing), so it
  # never reverts enrich's value.
  application = each.value.application

  # ── ADR-0003 enrichment, WIRED since provider 1.0.12-beta.4 ─────────────
  # v1.0.11 and 1.0.12-beta.3 accepted these and silently dropped them, which is
  # why `fwgitops enrich` existed. beta.4 writes them — verified in
  # spike/provider-beta4 by writing each and reading it back from SCM.
  #
  # `category` and `source_user` are set EXPLICITLY, following the provider's own
  # scm_security_rule example. Omission is not "leave alone": source_user is
  # optional-NOT-computed, so absent config means REMOVE, which is what made
  # every prod-edge plan want to null it.
  description        = each.value.description
  log_start          = each.value.log_start
  source_user        = each.value.source_user
  category           = each.value.category
  negate_source      = each.value.negate_source
  negate_destination = each.value.negate_destination

  log_setting = each.value.log_setting

  # The provider takes a nested object; the compiler carries a single group name.
  profile_setting = each.value.profile_group == null ? null : {
    group = [each.value.profile_group]
  }

  # ORDERING IS DELIBERATELY NOT WIRED YET, and not because it does not work.
  # `position` (the rulebase, pre|post) and `relative_position` (top|bottom|
  # before|after) are both honoured by beta.4 — verified in spike/beta4-ordering.
  #
  # The hazard is applying them to rules that ALREADY EXIST. The compiler
  # defaults every rule to relative_position="bottom", so wiring it would send a
  # move for five live rules at once, and rule order IS policy: a permissive rule
  # above a deny is a different firewall. What that does to an existing rulebase
  # has not been tested, and it does not need to ride along with the field fix
  # below, which is what closes the profile_setting gap.
  #
  # PROBED 2026-08-04 (spike/ordering-existing) and the answer is DO NOT WIRE IT.
  # A first-time add of relative_position="bottom" RE-STACKS the rulebase:
  #
  #   before: charlie, bravo, alpha
  #   after:  alpha, charlie, bravo
  #
  # and not into for_each order either (alphabetical would be alpha, bravo,
  # charlie) — each move-to-bottom lands in whatever order Terraform processes
  # the map, which is not a guaranteed stable ordering. The plan shows only
  # `+ relative_position = "bottom"`, so policy is rewritten silently.
  #
  # The mechanism itself is fine: a no-change value is a no-op, and changing the
  # value moves the rule cleanly. It is the compiler's BLANKET DEFAULT that is
  # unsafe. Wiring this needs the compiler to emit relative_position only when
  # the intent explicitly asked for a position, which the intent model cannot
  # express today (`position` defaults to "bottom", so "unspecified" and
  # "deliberately bottom" are the same value).
  #
  # NOTE Terraform also cannot SEE ordering drift: relative_position is a
  # create/update instruction, not a stored property, so a rule reordered
  # out-of-band produces `No changes` on the next plan.

  # `target_rule` is DELIBERATELY NOT WIRED, and cannot be from here.
  #
  # The provider wants a UUID — "UUID of the rule to position this rule relative
  # to" — so the compiler's anchor rule KEY would have to resolve to
  # `scm_security_rule.this[<key>].id`. That is a self-reference inside this very
  # for_each block, and Terraform rejects it:
  #
  #   Error: Cycle: module.security_folder.scm_security_rule.this["REQ-..."],
  #                 module.security_folder.scm_security_rule.this["REQ-..."], ...
  #
  # `top` / `bottom` need no anchor and ARE honoured through relative_position
  # above. Only before/after ordering is affected, and it stays with
  # `fwgitops enrich`, which resolves the anchor to a UUID over REST after apply.
  #
  # Do not "fix" this by passing the anchor's NAME: the move then 404s with
  # "Failed to find obj-uuid" WHILE THE APPLY REPORTS SUCCESS, so the rule lands
  # in the wrong position and the pipeline stays green (verified,
  # spike/beta4-ordering).
}

# ── Zones (ZoneRequest, ADR-0001 kind #2) ─────────────────────────────────
# scm_zone has NO `tag` attribute (unlike scm_security_rule), so zones cannot
# carry the `gitops:` provenance markers and are invisible to drift.py's
# tag-based detection. That is a property of the provider, not an oversight —
# only 14 of its resources are taggable. Recorded in ADR-0001.
resource "scm_zone" "this" {
  for_each = var.zones

  name = each.value.name

  # Exactly one is non-null. See the `zones` variable.
  folder = each.value.folder
  device = each.value.device

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
  # TWO SOURCES, ONE RESOURCE. `interfaces` is compiled from InterfaceRequests
  # (device scope, addressed); `folder_interfaces` is materialised from the
  # catalog (folder scope, `$`-prefixed variables, no addressing). The key
  # spaces are disjoint by construction — a folder variable is always
  # `$`-prefixed and a device interface never is — so the merge cannot silently
  # drop either side. `fwgitops folder-interfaces` asserts that prefix.
  for_each = merge(var.folder_interfaces, var.interfaces)

  name = each.value.name

  # Exactly one is non-null. A device-scope write to an interface inherited from
  # a parent folder creates a per-device OVERRIDE, leaving the shared object and
  # the other firewall untouched (spike/device-override-probe).
  folder = each.value.folder
  device = each.value.device

  # The physical port this folder-scope VARIABLE resolves to. Only meaningful at
  # folder scope; null on a device-scope override, where the object already IS
  # the port. Wiring it is what lets a NEW folder have bindable interfaces at
  # all — see ADR-0005 and `fwgitops folder-interfaces`.
  default_value = each.value.default_value

  comment = each.value.comment
  layer3  = each.value.layer3
}

# ── Logical routers (RouteRequest, ADR-0001 kind #4) ──────────────────────
# scm_logical_router has no `tag` attribute, so routers use state-based drift
# like zones and interfaces. Note this resource owns the WHOLE router including
# VRF interface membership — see the `routers` variable.
resource "scm_logical_router" "this" {
  for_each = var.routers

  name = each.value.name

  # Exactly one is non-null. See the `routers` variable.
  folder = each.value.folder
  device = each.value.device
  vrf    = each.value.vrf
}
