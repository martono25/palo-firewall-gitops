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

# ── Tag objects: CREATED ELSEWHERE, DESTROYED ELSEWHERE (ADR-0009) ────────
#
# `scm_tag` used to live here, for_each over every tag any object or rule used.
# That made Terraform own the whole lifecycle — and MEASURED 2026-08-10
# (spike/tag-destroy-ordering), changing one tag VALUE on a live rule failed the
# apply: Terraform ran the tag DESTROY before the rule UPDATE that released it,
# and SCM refused with 409 NON_ZERO_REFS. Once the rule's config no longer
# REFERENCES the old tag, nothing orders the destroy after the update — the edge
# that existed for creation is gone exactly when destruction needs it.
#
# Neither workaround survives review: `-target` on the rules pulls the tag in and
# plans the destroy anyway, and `depends_on = [scm_tag.this]` is the pattern this
# module REMOVED once already, because it made every rule depend on every object
# and a `destroy -target` of one address cascaded into destroying ALL rules.
#
# So the halves are separated in time (ADR-0009):
#
#   fwgitops ensure-tags   before apply — create what is missing
#   terraform apply + push
#   fwgitops sweep-tags    after push   — remove what nothing references
#
# Tags are still REFERENCED by name below. The API validates them as references
# and rejects free-form strings (INVALID_REFERENCE), so `ensure-tags` running
# first is load-bearing, not a convenience.

# FORGET the tag objects Terraform already manages; do NOT destroy them. Without
# this, the first apply after the change above would try to destroy every tag at
# once — and 409 on all of them, since the rules still reference them.
removed {
  from = scm_tag.this

  lifecycle {
    destroy = false
  }
}

# Address and service objects join the tag lifecycle for the SAME measured
# reason, four days later and one object class along (ADR-0010). Widening a live
# rule's destination on 2026-08-13 planned an in-place rule update plus a destroy
# of the address the old value released, ran the DESTROY FIRST, and SCM refused
# with 409 NON_ZERO_REFS while the rule still pointed at it.
#
# The rule was never being destroyed. Object names are content-addressed
# (`addr-` + sha256(value)[:10]), so a changed value is a DIFFERENT object rather
# than an edited one — which is what lets three rules share one `10.20.1.0/24`.
# Only the garbage collection of the released object failed.
#
# Documented Terraform behaviour, not a provider defect: update-before-destroy
# ordering is guaranteed only when the child is RECREATED under
# `create_before_destroy` (hashicorp/terraform#32136), and this is a pure delete.
#
# Same separation in time, same load-bearing ordering — the API validates these
# as references and rejects free-form strings, so `objects ensure` running first
# is required, not a convenience:
#
#   fwgitops objects ensure   before apply — create what is missing
#   terraform apply + push
#   fwgitops objects sweep    after push   — remove what nothing references
removed {
  from = scm_address.this

  lifecycle {
    destroy = false
  }
}

removed {
  from = scm_service.this

  lifecycle {
    destroy = false
  }
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
  # Plain names now, not resource references: these objects are no longer
  # Terraform-managed (ADR-0010). The fine-grained edges these expressions used
  # to create are what ordered object-create before rule-create — `objects
  # ensure` provides that ordering instead, by running to completion first.
  source      = each.value.sources
  destination = each.value.destinations
  service     = each.value.services
  tag         = each.value.tags

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

  # ORDERING — WIRED as of v1.41.0, and safe only because of what changed in the
  # compiler alongside it.
  #
  # The hazard was never the mechanism. PROBED 2026-08-04
  # (spike/ordering-existing): a FIRST-TIME add of relative_position="bottom"
  # RE-STACKS an existing rulebase —
  #
  #   before: charlie, bravo, alpha
  #   after:  alpha, charlie, bravo
  #
  # and not into for_each order either (alphabetical would be alpha, bravo,
  # charlie): each move-to-bottom lands in whatever order Terraform processes the
  # map, which is not a guaranteed stable ordering. The plan showed only
  # `+ relative_position = "bottom"`, so policy would have been rewritten
  # silently, for every rule at once. Rule order IS policy — a permissive rule
  # above a deny is a different firewall.
  #
  # What made that unsafe was the COMPILER'S BLANKET DEFAULT, not this argument:
  # `position` defaulted to "bottom", so every rule carried a value nobody asked
  # for. Since v1.41.0 an unspecified position is null all the way down, so a
  # rule nobody positioned sends NOTHING and is never moved. Only a rule whose
  # intent explicitly says `position:` carries a value here.
  #
  # The supporting findings still hold: a no-change value is a no-op (Terraform
  # does not act), and changing the value moves the rule cleanly.
  #
  # NOTE Terraform still cannot SEE ordering drift: relative_position is a
  # create/update instruction, not a stored property, so a rule reordered
  # out-of-band produces `No changes` on the next plan. Wiring this does not fix
  # that, and nothing here should be read as claiming it does.
  relative_position = each.value.relative_position

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
