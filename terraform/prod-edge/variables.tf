variable "folder" {
  description = "SCM folder for this state (one state per folder)."
  type        = string
  default     = "prod-edge"
}

# These variables are auto-populated from rules.auto.tfvars.json (written by
# `fwgitops compile`) because Terraform auto-loads *.auto.tfvars.json. Defaults
# to empty so `plan` works for a folder with no rules yet.
#
# Types mirror the module's — kept in sync with src/fwgitops/compiler.py.

variable "address_objects" {
  type = map(object({
    name   = string
    type   = string
    value  = string
    folder = string
    tags   = list(string)
  }))
  default = {}
}

variable "service_objects" {
  type = map(object({
    name     = string
    protocol = string
    port     = string
    folder   = string
    tags     = list(string)
  }))
  default = {}
}

variable "security_rules" {
  type = map(object({
    name         = string
    folder       = string
    from_zones   = list(string)
    to_zones     = list(string)
    sources      = list(string)
    destinations = list(string)
    services     = list(string)
    action       = string
    log_end      = bool
    disabled     = optional(bool, false)
    tags         = list(string)
    # ── ADR-0003 rule components ──────────────────────────────────────────
    # HOLE 3. These were missing here while the module declared them and the
    # compiler emitted them (`_rule_dict`). Terraform's object-to-object
    # conversion SILENTLY DISCARDS attributes the target type does not declare
    # — no warning, no diagnostic, exit 0 — so the module fell back to its own
    # `optional(...)` defaults and the intent's App-ID / profile / log setting
    # never arrived. tfcontract compares top-level KEY names, so it saw
    # `security_rules` declared and wired and reported green.
    #
    # Keep this block byte-identical to terraform/modules/security_folder/
    # variables.tf. A schema-level contract check is tracked in TODOS.md.
    application       = optional(list(string), ["any"])
    profile_group     = optional(string)           # null -> no security profile
    log_setting       = optional(string)           # null -> local logs only
    rulebase          = optional(string, "pre")
    relative_position = optional(string, "bottom") # top|bottom|before|after
    target_rule       = optional(string)           # anchor for before/after
  }))
  default = {}
}

# ── ZoneRequest (ADR-0001 kind #2) ────────────────────────────────────────
# Byte-identical to terraform/modules/security_folder/variables.tf. HOLE 3:
# Terraform SILENTLY DISCARDS object attributes the target type does not
# declare, so a root type narrower than the module's drops fields with no
# diagnostic. `fwgitops compile` now asserts this per-attribute (ADR-0004), so
# a drift here fails the compile rather than shipping a half-configured zone.
variable "zones" {
  description = "Map of zone name -> zone definition (ZoneRequest, ADR-0001)."
  type = map(object({
    name   = string
    folder = string
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
# Byte-identical to the module's. HOLE 3: Terraform silently discards object
# attributes the target type does not declare, at ANY depth — and `layer3` is
# nested, which is exactly where that bites. The compile-time contract check
# asserts this per attribute path (ADR-0004).
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
    name    = string
    folder  = string
    comment = optional(string)
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
# Byte-identical to the module's. HOLE 3 applies at every level of this type,
# and it is four deep — the contract check asserts each dotted path.
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
    name   = string
    folder = string
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
