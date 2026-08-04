# What does setting `relative_position` do to rules that ALREADY EXIST?
#
# spike/beta4-ordering answered "create a rule in position" — top, bottom and
# before+target_rule all land correctly on CREATE. That does not answer this,
# and this is the one that gates wiring ordering into the module:
#
#   the compiler defaults EVERY rule to relative_position = "bottom"
#
# so wiring it would send a move for every existing rule at once. If a "bottom"
# on an existing rule is a no-op, that is harmless. If it re-stacks the rulebase
# in for_each order, it silently rewrites policy — a permissive rule landing
# above a deny is a different firewall, and nothing in the plan says so.
#
# PHASES, driven by -var ordering_mode=:
#   none        rules created with NO relative_position -> creation order
#   all_bottom  the compiler's default applied to all three existing rules
#   one_top     one existing rule asks to jump to the top
#
# BLAST RADIUS: folder `GitOps` — zero devices, nothing inherits from it, never
# pushed. All objects prefixed, destroyed after readback.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "1.0.12-beta.4"
    }
  }
}

provider "scm" {}

variable "folder" {
  type    = string
  default = "GitOps"
  validation {
    condition     = !contains(["prod-edge", "ngfw-shared", "All"], var.folder)
    error_message = "Refusing to probe in a folder that feeds production devices."
  }
}

variable "ordering_mode" {
  type    = string
  default = "none"
  validation {
    condition     = contains(["none", "all_bottom", "one_top"], var.ordering_mode)
    error_message = "ordering_mode must be none | all_bottom | one_top."
  }
}

locals {
  # Which relative_position each rule asks for, per phase. null = attribute
  # absent from config entirely, which is how the rules are first created.
  pos = {
    none       = { alpha = null,     bravo = null,     charlie = null }
    all_bottom = { alpha = "bottom", bravo = "bottom", charlie = "bottom" }
    one_top    = { alpha = "bottom", bravo = "top",    charlie = "bottom" }
  }[var.ordering_mode]
}

resource "scm_address" "probe" {
  name       = "fwgitops-oe-src"
  folder     = var.folder
  ip_netmask = "10.99.95.0/24"
}

resource "scm_security_rule" "r" {
  for_each = toset(["alpha", "bravo", "charlie"])

  name        = "fwgitops-oe-${each.key}"
  folder      = var.folder
  from        = ["any"]
  to          = ["any"]
  source      = [scm_address.probe.name]
  destination = ["any"]
  service     = ["any"]
  action      = "allow"
  application = ["any"] # required by the API on every write

  relative_position = local.pos[each.key]
}
