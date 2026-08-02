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
