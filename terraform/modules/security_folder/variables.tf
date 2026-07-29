variable "folder" {
  description = "SCM folder these objects/rules live in. Also scopes the scm_tag objects."
  type        = string
}

# Variable types mirror the compiler's rules.auto.tfvars.json contract exactly
# (src/fwgitops/compiler.py :: to_tfvars). This part IS verified — it is the
# stable interface between the Python compiler and Terraform.

variable "address_objects" {
  description = "Map of address object name -> definition (from the compiler)."
  type = map(object({
    name   = string
    type   = string # "ip-netmask" | "fqdn"
    value  = string
    folder = string
    tags   = list(string)
  }))
  default = {}
}

variable "service_objects" {
  description = "Map of service object name -> definition."
  type = map(object({
    name     = string
    protocol = string # "tcp" | "udp"
    port     = string # "443" or "8000-8100"
    folder   = string
    tags     = list(string)
  }))
  default = {}
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
    application       = optional(list(string), ["any"])
    profile_group     = optional(string)      # null -> no security profile
    log_setting       = optional(string)      # null -> local logs only
    rulebase          = optional(string, "pre")
    relative_position = optional(string, "bottom") # top|bottom|before|after
    target_rule       = optional(string)      # anchor for before/after
  }))
  default = {}
}
