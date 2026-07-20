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
  }))
  default = {}
}
