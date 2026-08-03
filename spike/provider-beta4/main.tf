# Does scm provider 1.0.12-beta.4 WRITE the security-rule fields that 1.0.11 and
# 1.0.12-beta.3 silently drop?
#
# ADR-0003 established that the provider ACCEPTS `application`, `profile_setting`,
# `log_setting` and ordering on scm_security_rule, reports success, treats them as
# computed, and never writes them — which is the entire reason
# `src/fwgitops/enrich.py` exists. That was confirmed on v1.0.11 AND
# v1.0.12-beta.3. beta.4 has since shipped and has not been tested.
#
# If beta.4 writes them, `enrich` can retire for those fields and the compiler
# can own them directly. That is a real simplification, so it is worth probing
# rather than assuming — in either direction.
#
# SECOND QUESTION, and the subtler one. `log_setting` is `computed` in the
# schema, and a plan against prod-edge shows:
#
#   ~ log_setting = "Cortex Data Lake" -> (known after apply)
#
# `computed` does NOT mean "preserved". It means Terraform records whatever the
# provider returns — so if writing the rule WITHOUT log_setting causes SCM to
# clear it, Terraform records the clearing silently and the plan gave no warning.
# Omitting the value should mean null. TEST 2 below settles that.
#
# BLAST RADIUS: folder `GitOps` — zero devices, nothing inherits from it. Every
# object is new and prefixed. Nothing is ever pushed.

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

# TEST 2 flips this. `true` writes log_setting/profile_setting; `false` omits
# them entirely, which is what asks "does omission clear what is already there?"
variable "set_enriched_fields" {
  type    = bool
  default = true
}

resource "scm_tag" "probe" {
  name   = "fwgitops-beta4-probe"
  folder = var.folder
}

resource "scm_address" "probe" {
  name       = "fwgitops-beta4-src"
  folder     = var.folder
  ip_netmask = "10.99.97.0/24"
  tag        = [scm_tag.probe.name]
}

resource "scm_service" "probe" {
  name     = "fwgitops-beta4-svc"
  folder   = var.folder
  protocol = { tcp = { port = "8443" } }
  tag      = [scm_tag.probe.name]
}

resource "scm_security_rule" "probe" {
  name   = "fwgitops-beta4-probe"
  folder = var.folder

  from        = ["any"]
  to          = ["any"]
  source      = [scm_address.probe.name]
  destination = ["any"]
  service     = [scm_service.probe.name]
  action      = "allow"
  log_end     = true

  # ── The fields ADR-0003 says are dropped ──────────────────────────────
  application     = var.set_enriched_fields ? ["web-browsing"] : ["any"]
  log_setting     = var.set_enriched_fields ? "Cortex Data Lake" : null
  profile_setting = var.set_enriched_fields ? { group = ["best-practice"] } : null
}

output "what_terraform_thinks_it_wrote" {
  value = {
    application     = scm_security_rule.probe.application
    log_setting     = scm_security_rule.probe.log_setting
    profile_setting = scm_security_rule.probe.profile_setting
  }
}
