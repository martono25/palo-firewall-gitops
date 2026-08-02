# THE DECISIVE QUESTION: writing an EXISTING inherited interface at device
# scope — does it create a per-device override, or mutate the object every
# device inherits?
#
# `spike/device-scope-probe` proved `device=` isolates for a NEW object. It says
# nothing about this, because the interfaces this tenant actually uses project
# the SAME object id at both scopes:
#
#   folder=ngfw-shared   $eth-local (default_value: ethernet1/4)   35479f59-…
#   device=<serial>      ethernet1/4                               35479f59-…
#
# ADR-0005 says InterfaceRequest CONFIGURES interfaces that already exist, so
# this is the path it actually takes. If the write mutates the shared object,
# `device:` targeting cannot isolate interface addressing — the change most
# likely to break connectivity — and the v2.0 plan changes.
#
# BLAST RADIUS — this one genuinely touches an object `prod-edge` inherits.
# Controls:
#   * target is the DISCONNECTED firewall (007955000893662). SCM config reaches
#     a device only on PUSH, and a disconnected device cannot be pushed to. This
#     probe never pushes.
#   * MTU ONLY. No addressing. `layer3.ip` stays empty, so even if this did
#     reach a device it could not change what the interface answers on. MTU 1476
#     is distinctive (1500 - 24) and easy to spot in a readback.
#   * the revert target is exact and known: `layer3: {}` on every scope, captured
#     as a baseline before applying.
#   * destroy immediately after readback, then verify against that baseline.
# Do NOT add `ip` here, and do NOT point it at the connected firewall.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0"
    }
  }
}

provider "scm" {}

variable "device" {
  type        = string
  default     = "007955000893662" # DISCONNECTED as of 2026-08-02
  description = "Target device serial. Must not be the connected firewall."
  validation {
    condition     = var.device != "007955000894453"
    error_message = "Refusing to probe against the CONNECTED firewall (007955000894453)."
  }
}

variable "name" {
  type        = string
  default     = "ethernet1/4" # $eth-local at folder scope — the shared object
  description = "The EXISTING inherited interface to write at device scope."
}

variable "mtu" {
  type        = number
  default     = 1476
  description = "Distinctive, harmless marker. MTU only — never addressing."
}

resource "scm_ethernet_interface" "probe" {
  name   = var.name
  device = var.device

  # MTU only. Deliberately no `ip` — see the blast-radius note.
  layer3 = {
    mtu = var.mtu
  }
}

output "probe_id" {
  description = "If this equals the SHARED object id (35479f59-…), the write went to the inherited object, not an override."
  value       = scm_ethernet_interface.probe.id
}

output "scope_fields" {
  value = {
    folder = scm_ethernet_interface.probe.folder
    device = scm_ethernet_interface.probe.device
  }
}
