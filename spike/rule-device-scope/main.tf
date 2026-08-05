# Can a SECURITY RULE be created at DEVICE scope?
#
# THE QUESTION v2.0 RULE PROVISIONING TURNS ON. Today an AccessRequest cannot
# name a target at all: `_ACCESS_SPEC_KEYS` has `environment` and neither
# `folder` nor `device`, `_target()` returns `(folder, None)`, and the rule is
# created at FOLDER scope where every firewall beneath inherits it. Whether
# per-firewall rules are even possible decides whether "target one firewall" is a
# design option or a dead end — and everything downstream of that.
#
# DO NOT ASSUME THE DOCS. The registry documents `device` for scm_security_rule
# exactly as it does for scm_zone, and SCM refuses the zone at write time with a
# misleading "Device <serial> doesn't exist" (spike/zone-device-scope).
# scm_logical_router behaves the same way (spike/router-device-naming). TWO of
# four Day-1 resources contradict their own documentation here, so folder-only is
# the DEFAULT assumption and this probe exists to overturn or confirm it.
#
# THE CONTROL IS NOT OPTIONAL. A bare rejection has two readings — "device scope
# is refused for RULES" and "device scope is refused on this firewall" — and they
# imply different builds. `control.tf` writes an ethernet interface at the same
# scope, on the same firewall, in the same apply. Without it the result is an
# anecdote.
#
# ── BLAST RADIUS ──────────────────────────────────────────────────────────
# This targets the CONNECTED production-pilot firewall, because it is now the
# only registered device (007955000893662 left SCM on 2026-08-05), so there is no
# disconnected firewall to probe against any more. Controls:
#
#   * LOCAL state — cannot disturb the S3-backed prod-edge or device roots.
#   * disabled = true — the rule cannot match a packet even if something pushed
#     it. This is the primary control.
#   * action = deny with TEST-NET-2/TEST-NET-3 addresses (RFC 5737,
#     documentation-only ranges that appear nowhere in this estate), so even
#     ENABLED it would match nothing.
#   * NO PUSH. SCM config reaches a device only on push, and this probe never
#     pushes. Staged config is inert.
#   * destroy immediately, then read back to confirm absence.
#
# Do NOT set disabled=false, and do NOT add a real CIDR.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "1.0.12-beta.4" # the version the real roots pin
    }
  }
}

provider "scm" {}

variable "device" {
  type        = string
  default     = "007955000894453" # fw-prod-edge-4453, CONNECTED
  description = "Target firewall serial."
}

variable "rule_name" {
  type    = string
  default = "fwgitops-probe-devrule"

  validation {
    condition     = can(regex("^fwgitops-probe-", var.rule_name))
    error_message = "Probe rule names must start with fwgitops-probe- so they are never mistaken for policy."
  }
}

variable "run_rule" {
  type        = bool
  default     = true
  description = "false runs the CONTROL alone, isolating a rule rejection from device scope generally."
}

resource "scm_security_rule" "probe" {
  count = var.run_rule ? 1 : 0

  name   = var.rule_name
  device = var.device

  # Zones the firewall inherits from prod-edge. Naming real zones keeps the
  # request valid, so a rejection is about SCOPE rather than a bad reference —
  # SCM reference-validates and would otherwise fail for the wrong reason.
  from = ["local"]
  to   = ["internet"]

  # RFC 5737 documentation ranges. Present nowhere in this estate, so this rule
  # matches nothing even if enabled.
  source      = ["198.51.100.0/24"]
  destination = ["203.0.113.0/24"]

  application = ["any"]
  service     = ["any"]

  # Set EXPLICITLY, following the provider's own example: these are
  # optional-NOT-computed, so omitting them means REMOVE rather than "leave
  # alone" — the lesson from the profile_setting incident (v1.15.0).
  category    = ["any"]
  source_user = ["any"]

  action = "deny"

  # THE PRIMARY SAFETY CONTROL. A disabled rule cannot match a packet.
  disabled = true

  description = "fwgitops device-scope probe. Inert: disabled, deny, TEST-NET only. Delete on sight."
}

output "rule_id" {
  value       = one(scm_security_rule.probe[*].id)
  description = "Non-null means SCM accepts a rule at device scope."
}

output "device" {
  value = var.device
}
