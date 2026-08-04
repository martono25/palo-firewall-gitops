# Can a ZONE be created at DEVICE scope, and does it reach hardware?
#
# TWO QUESTIONS, and the second is the one that matters:
#
#   Q1. Does the provider create an scm_zone with `device = <serial>`?
#   Q2. Does a zone declared by GitOps appear in the DEVICE's running config?
#
# Q2 is the open item. `ZoneRequest` has been built since v1.2.0 and has NEVER
# reached a firewall: every zone this tenant uses (`local`, `internet`) is
# pre-defined in `ngfw-shared` and self-attaches by inheritance, so a green
# apply proves the chain, not the kind. A zone nobody has watched land on
# hardware is a zone nobody has verified.
#
# ── WHY THIS PROBE EXISTS AT ALL: A RAW REST CALL SAID NO ─────────────────
# Calling POST /config/network/v1/zones?device=<serial> directly returns:
#
#   400 API_I00013 "Device 007955000894453 doesn't exist.
#                   Please create it before running the command"
#
# which is the SAME misleading message `scm_logical_router` gives for a scope it
# genuinely does not support (spike/router-device-naming), so the tempting
# conclusion is "zones are folder-scope only, like routers".
#
# THAT CONCLUSION IS NOT SUPPORTED, and the docs are why. The registry page for
# scm_zone states plainly:
#
#   "Note: You must specify exactly one of device, folder or snippet."
#   device (String) The device in which the resource is defined
#
# and documents the device import form `terraform import scm_zone.example
# ::device:id`. A resource does not document an import form for a scope it
# rejects. There is also direct local evidence: THIS REPO already writes
# ethernet interfaces at device scope on this very firewall through this very
# provider (terraform/device-007955000894453), and the same raw REST call for
# interfaces is refused with the same message. So the message tracks the CALL,
# not the capability.
#
# The honest reading is that the raw call is wrong, not that SCM lacks the
# scope. This probe settles it against the provider, which is what the platform
# actually uses. Do NOT record "SCM rejects device-scope zones" anywhere on the
# strength of a hand-rolled REST call.
#
# ── BLAST RADIUS ──────────────────────────────────────────────────────────
#   * LOCAL state. Cannot disturb the S3-backed prod-edge or device states.
#   * EMPTY layer3. The zone binds no interface, so it cannot move a live
#     interface out of `local`/`internet` and cannot black-hole traffic. This
#     is the one place an empty zone is the right choice: the question here is
#     "does the object land", not "does it carry packets".
#   * DISTINCT name (`fwgitops-probe-dmz`) that matches nothing pre-existing.
#   * The push is a SEPARATE, explicit step (push.sh) — applying this file
#     alone stages config in SCM and reaches no device.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "1.0.12-beta.4" # the version prod-edge and device roots pin
    }
  }
}

provider "scm" {}

variable "device" {
  type        = string
  default     = "007955000894453" # fw-prod-edge-4453, CONNECTED
  description = <<-EOT
    Target firewall serial.

    This probe DOES target the connected firewall, deliberately and unlike
    spike/device-override-probe, because Q2 cannot be answered anywhere else:
    a disconnected firewall stages config and never commits, so it can prove a
    zone was WRITTEN but never that one ARRIVED. Arriving is the open question.
  EOT
}

variable "zone_name" {
  type    = string
  default = "fwgitops-probe-dmz"

  # A probe that reuses a real zone name could bind or unbind live interfaces
  # on commit. Keep the name obviously disposable.
  validation {
    condition     = can(regex("^fwgitops-probe-", var.zone_name))
    error_message = "Probe zone names must start with fwgitops-probe- so they are never confused with a real zone."
  }
}

variable "run_zone" {
  type        = bool
  default     = true
  description = "Set false to run the CONTROL alone, isolating the zone rejection from device scope generally."
}

resource "scm_zone" "probe" {
  count = var.run_zone ? 1 : 0

  name   = var.zone_name
  device = var.device

  network = {
    # EMPTY on purpose — see BLAST RADIUS. This probe asks whether the object
    # reaches the device, not whether it forwards packets.
    layer3 = []
  }
}

output "zone_id" {
  value       = one(scm_zone.probe[*].id)
  description = "SCM UUID. Feed to readback.py to confirm the scope it landed in."
}

output "device" {
  value = var.device
}
