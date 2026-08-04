# Does scm provider 1.0.12-beta.4 honour rule ORDERING?
#
# ADR-0003 found the provider silently drops `application`, `profile_setting`,
# `log_setting` AND ordering on scm_security_rule (v1.0.11, v1.0.12-beta.3).
# spike/provider-beta4 showed beta.4 now writes the first three. Ordering was
# part of the same finding and is the remaining unknown — and it is the one that
# cannot be checked by reading a field back, because ordering is not an attribute
# on the object. It is the rule's POSITION in the rulebase.
#
# So the probe creates rules whose requested order differs from their creation
# order, then reads the rulebase back and compares the SEQUENCE.
#
#   creation order:  alpha, bravo, charlie
#   requested order: bravo (top), charlie (before alpha), alpha (bottom)
#   expected result: bravo, charlie, alpha
#
# If ordering is dropped, the rulebase comes back in creation order instead.
# That matters because rule order IS policy: a permissive rule above a deny is a
# different firewall from the same two rules reversed, and nothing in a plan or
# an apply would show it.
#
# BLAST RADIUS: folder `GitOps` — zero devices, nothing inherits from it, never
# pushed. All objects prefixed and destroyed after readback.

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

resource "scm_address" "probe" {
  name       = "fwgitops-ord-src"
  folder     = var.folder
  ip_netmask = "10.99.96.0/24"
}

locals {
  base = {
    folder      = var.folder
    from        = ["any"]
    to          = ["any"]
    source      = [scm_address.probe.name]
    destination = ["any"]
    service     = ["any"]
    action      = "allow"
  }
}

# Created FIRST, wants to end up LAST.
resource "scm_security_rule" "alpha" {
  name              = "fwgitops-ord-alpha"
  folder            = local.base.folder
  from              = local.base.from
  to                = local.base.to
  source            = local.base.source
  destination       = local.base.destination
  service           = local.base.service
  action            = local.base.action
  # REQUIRED by the SCM API on every write (API_I00035 "application is required"),
  # regardless of whether the provider persists it. Same constraint the module
  # documents for the real rules.
  application       = ["any"]
  relative_position = "bottom"
}

# Created SECOND, wants to end up FIRST.
resource "scm_security_rule" "bravo" {
  name              = "fwgitops-ord-bravo"
  folder            = local.base.folder
  from              = local.base.from
  to                = local.base.to
  source            = local.base.source
  destination       = local.base.destination
  service           = local.base.service
  action            = local.base.action
  # REQUIRED by the SCM API on every write (API_I00035 "application is required"),
  # regardless of whether the provider persists it. Same constraint the module
  # documents for the real rules.
  application       = ["any"]
  relative_position = "top"

  depends_on = [scm_security_rule.alpha]
}

# Created THIRD, wants to sit BEFORE alpha (i.e. in the middle).
resource "scm_security_rule" "charlie" {
  name              = "fwgitops-ord-charlie"
  folder            = local.base.folder
  from              = local.base.from
  to                = local.base.to
  source            = local.base.source
  destination       = local.base.destination
  service           = local.base.service
  action            = local.base.action
  # REQUIRED by the SCM API on every write (API_I00035 "application is required"),
  # regardless of whether the provider persists it. Same constraint the module
  # documents for the real rules.
  application       = ["any"]
  relative_position = "before"
  target_rule       = scm_security_rule.alpha.name

  depends_on = [scm_security_rule.bravo]
}

output "positions" {
  value = {
    alpha   = scm_security_rule.alpha.position
    bravo   = scm_security_rule.bravo.position
    charlie = scm_security_rule.charlie.position
  }
}
