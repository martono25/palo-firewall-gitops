# ADR-0005 prerequisite 4 — does the scm provider WRITE scm_ethernet_interface
# fields, or silently drop them the way it drops profile_setting / log_setting /
# ordering on scm_security_rule (ADR-0003)?
#
# Fidelity varies PER RESOURCE TYPE — scm_zone turned out faithful while
# scm_security_rule is not — so it cannot be inferred. See spike/zone-probe.
#
# BLAST RADIUS. ADR-0005 assumed there was no clean scratch target, because the
# real interfaces live in `ngfw-shared`, which feeds prod-edge (2 devices) and
# GitOps. There is one: create a NEW interface, under a NEW name, in `GitOps`.
#   * GitOps has ZERO devices, and nothing inherits FROM it -> no device reached
#   * a new name is not an override of $eth-local / $eth-internet -> nothing
#     existing is shadowed or modified
# Do NOT change `folder` to ngfw-shared or prod-edge.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0"
    }
  }
}

# Auth from the environment (SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE).
provider "scm" {}

variable "folder" {
  type        = string
  default     = "GitOps"
  description = "Scratch folder. MUST have no devices — see the note above."
  validation {
    condition     = !contains(["prod-edge", "ngfw-shared", "All"], var.folder)
    error_message = "Refusing to probe in a folder that feeds production devices."
  }
}

variable "name" {
  type    = string
  default = "$eth-fwgitops-probe"
}

resource "scm_ethernet_interface" "probe" {
  name   = var.name
  folder = var.folder

  # Top-level scalar.
  comment = "fwgitops fidelity probe — safe to delete"

  # Nested object + nested list-of-objects, which is where HOLE 3 lives and
  # where `enrich` would be needed if the provider drops them.
  layer3 = {
    mtu = 1500
    ip  = [{ name = "10.99.99.1/30" }]
  }
}

output "probe_id" { value = scm_ethernet_interface.probe.id }

output "what_terraform_thinks_it_wrote" {
  description = "Compare against readback.py. Divergence == the provider dropped it."
  value = {
    name    = scm_ethernet_interface.probe.name
    comment = scm_ethernet_interface.probe.comment
    layer3  = scm_ethernet_interface.probe.layer3
  }
}
