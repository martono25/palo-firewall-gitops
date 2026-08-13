variable "folder" {
  description = "SCM folder these objects/rules live in. Also scopes the scm_tag objects."
  type        = string
}


variable "security_rules" {
  description = "Map of rule key (stable for_each key) -> rule definition."
  type = map(object({
    name         = string
    folder       = string
    from_zones   = list(string)
    to_zones     = list(string)
    sources      = list(string)
    destinations = list(string)
    services     = list(string)
    action       = string # "allow" | "deny"
    log_end      = bool
    disabled     = optional(bool, false)
    tags         = list(string)
    # ── ADR-0003 rule components (optional; defaults = plain L4 allow) ──
    application   = optional(list(string), ["any"])
    profile_group = optional(string) # null -> no security profile
    log_setting   = optional(string) # null -> local logs only
    rulebase      = optional(string, "pre")
    # NO DEFAULT. `optional(string, "bottom")` substitutes the default when the
    # value is NULL, so an unspecified position was silently turned back into
    # "bottom" AT THE MODULE BOUNDARY — and a first-time write of a concrete
    # position RE-STACKS the rulebase (spike/ordering-existing). Caught by a plan
    # against the live folder on 2026-08-09: `+ relative_position = "bottom"` on
    # all five rules, which is precisely the silent policy rewrite the spike
    # warned about. The compiler's null must survive to the provider.
    relative_position = optional(string) # top|bottom (before/after: see enrich)
    target_rule       = optional(string) # anchor for before/after
    # ── v1.0 rule completeness ──
    # Set explicitly, per the provider's own scm_security_rule example. Omitting
    # `category` / `source_user` is not "leave alone": the provider models
    # source_user as optional-NOT-computed, so absent config means REMOVE.
    description        = optional(string)
    log_start          = optional(bool, false)
    source_user        = optional(list(string), ["any"])
    category           = optional(list(string), ["any"])
    negate_source      = optional(bool, false)
    negate_destination = optional(bool, false)
  }))
  default = {}
}

# ── ZoneRequest (ADR-0001 kind #2) ────────────────────────────────────────
# Shape mirrors the scm_zone provider schema EXACTLY (v1.0.11), so a reader can
# diff this against `terraform providers schema -json` without translating:
# zone_protection_profile / log_setting live INSIDE `network`; the User-ID and
# device-ID toggles and the ACLs are top-level.
#
# Verified live 2026-07-31: the provider writes these faithfully and SCM
# reference-validates the profile names fail-closed. Unlike scm_security_rule,
# zones need no `enrich` post-pass (ADR-0004).
variable "zones" {
  description = "Map of zone name -> zone definition (ZoneRequest, ADR-0001)."
  type = map(object({
    name = string
    # Exactly one of folder/device is non-null (the provider enforces "exactly
    # one of device, folder, snippet"). A firewall is the last level of the SCM
    # hierarchy and inherits down it, but is addressed `device=`, never
    # `folder=`. A device-scope write to an inherited object creates a
    # per-device OVERRIDE — verified in spike/device-override-probe.
    folder = optional(string)
    device = optional(string)
    network = optional(object({
      layer2       = optional(list(string))
      layer3       = optional(list(string))
      external     = optional(list(string))
      tap          = optional(list(string))
      virtual_wire = optional(list(string))
      # ── security posture (ADR-0003 equivalent for zones) ──────────────
      # A zone with no protection profile has NO flood or reconnaissance
      # protection. The risk classifier flags that; it is not silently fine.
      zone_protection_profile         = optional(string)
      log_setting                     = optional(string)
      enable_packet_buffer_protection = optional(bool)
    }))
    # User-ID must be enabled PER ZONE or a rule matching on `source_user`
    # never matches — the rule model has supported source_user since v1.0.
    enable_user_identification   = optional(bool)
    enable_device_identification = optional(bool)
    dos_profile                  = optional(string)
    dos_log_setting              = optional(string)
    user_acl = optional(object({
      include_list = optional(list(string))
      exclude_list = optional(list(string))
    }))
    device_acl = optional(object({
      include_list = optional(list(string))
      exclude_list = optional(list(string))
    }))
  }))
  default = {}
}

# ── InterfaceRequest (ADR-0001 kind #3) ───────────────────────────────────
# Shape mirrors the scm_ethernet_interface provider schema (v1.0.11). Verified
# live 2026-08-02 that the provider writes these faithfully, including the
# nested `layer3.ip` list-of-objects — so no `enrich` pass is needed.
#
# ADR-0005: this CONFIGURES an existing interface. On the tenant the interfaces
# already exist as folder-scope variables (`$eth-local`) with `layer3` empty;
# what an InterfaceRequest supplies is the addressing.
#
# EXACTLY ONE of `ip` / `dhcp_client` is non-null — the provider requires it and
# the intent loader rejects violations at PR time.
variable "interfaces" {
  description = "Map of interface name -> configuration (InterfaceRequest, ADR-0001)."
  type = map(object({
    name = string
    # Exactly one of folder/device is non-null (the provider enforces "exactly
    # one of device, folder, snippet"). A firewall is the last level of the SCM
    # hierarchy and inherits down it, but is addressed `device=`, never
    # `folder=`. A device-scope write to an inherited object creates a
    # per-device OVERRIDE — verified in spike/device-override-probe.
    folder = optional(string)
    device = optional(string)
    # FOLDER-SCOPE VARIABLES ONLY. On this tenant a folder-scope interface is a
    # `$`-prefixed VARIABLE whose default_value names the physical port every
    # firewall beneath the folder resolves it to. A zone can only bind an
    # interface object that exists at its scope -- binding a literal port name is
    # refused as an invalid reference -- so this attribute is what makes a
    # folder's zones bindable at all. Null at device scope, where the object IS
    # the physical port.
    default_value = optional(string)
    comment       = optional(string)
    layer3 = optional(object({
      ip          = optional(list(object({ name = string })))
      dhcp_client = optional(object({ enable = optional(bool) }))
      mtu         = optional(number)
      # Which admin services answer here. Attaching one to a DATA interface
      # exposes admin services on that network — absence is the safe default.
      interface_management_profile = optional(string)
    }))
  }))
  default = {}
}

# ── RouteRequest (ADR-0001 kind #4) ───────────────────────────────────────
# Routes AGGREGATE: many RouteRequests become one logical router, because a
# static route lives at vrf[].routing_table.ip.static_route[] and that same
# object carries the VRF's interface membership.
#
# `interface` is therefore NOT optional in spirit: Terraform manages whole
# objects, so writing this router without it would strip the interface list from
# the object all traffic traverses. The compiler carries membership on every
# compiled route (from catalog/routers.yaml) and asserts they agree.
variable "folder_interfaces" {
  description = <<-EOT
    Folder-scope `$`-interface VARIABLES, from catalog/interfaces.yaml via
    `fwgitops folder-interfaces`. SEPARATE from `interfaces` because both arrive
    as auto-loaded .tfvars and Terraform REPLACES a variable set twice rather
    than merging it — one variable name would let whichever file loads last
    erase the other, with no diagnostic.
  EOT
  type = map(object({
    name = string
    # Exactly one of folder/device is non-null (the provider enforces "exactly
    # one of device, folder, snippet"). A firewall is the last level of the SCM
    # hierarchy and inherits down it, but is addressed `device=`, never
    # `folder=`. A device-scope write to an inherited object creates a
    # per-device OVERRIDE — verified in spike/device-override-probe.
    folder = optional(string)
    device = optional(string)
    # FOLDER-SCOPE VARIABLES ONLY. On this tenant a folder-scope interface is a
    # `$`-prefixed VARIABLE whose default_value names the physical port every
    # firewall beneath the folder resolves it to. A zone can only bind an
    # interface object that exists at its scope -- binding a literal port name is
    # refused as an invalid reference -- so this attribute is what makes a
    # folder's zones bindable at all. Null at device scope, where the object IS
    # the physical port.
    default_value = optional(string)
    comment       = optional(string)
    layer3 = optional(object({
      ip          = optional(list(object({ name = string })))
      dhcp_client = optional(object({ enable = optional(bool) }))
      mtu         = optional(number)
      # Which admin services answer here. Attaching one to a DATA interface
      # exposes admin services on that network — absence is the safe default.
      interface_management_profile = optional(string)
    }))
  }))
  default = {}
}

# ── RouteRequest (ADR-0001 kind #4) ───────────────────────────────────────
# Routes AGGREGATE: many RouteRequests become one logical router, because a
# static route lives at vrf[].routing_table.ip.static_route[] and that same
# object carries the VRF's interface membership.
#
# `interface` is therefore NOT optional in spirit: Terraform manages whole
# objects, so writing this router without it would strip the interface list from
# the object all traffic traverses. The compiler carries membership on every
# compiled route (from catalog/routers.yaml) and asserts they agree.
variable "routers" {
  description = "Map of logical router name -> definition (RouteRequest, ADR-0001)."
  type = map(object({
    name = string
    # Exactly one of folder/device is non-null (the provider enforces "exactly
    # one of device, folder, snippet"). A firewall is the last level of the SCM
    # hierarchy and inherits down it, but is addressed `device=`, never
    # `folder=`. A device-scope write to an inherited object creates a
    # per-device OVERRIDE — verified in spike/device-override-probe.
    folder = optional(string)
    device = optional(string)
    vrf = optional(list(object({
      name      = string
      interface = optional(list(string))
      routing_table = optional(object({
        ip = optional(object({
          static_route = optional(list(object({
            name        = string
            destination = optional(string)
            nexthop = optional(object({
              ip_address = optional(string)
            }))
            interface  = optional(string)
            metric     = optional(number)
            admin_dist = optional(number)
          })))
        }))
      }))
    })))
  }))
  default = {}
}
