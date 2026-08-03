# WHICH NAME does a DEVICE-scope logical router use for its VRF membership —
# the SCM folder variable ($eth-local) or the physical port (ethernet1/4)?
#
# This is the last blocker on RouteRequest reaching hardware. A route lives four
# levels inside scm_logical_router, and that same object carries the VRF's
# interface list. Terraform manages whole objects, so writing the router with
# the WRONG interface names does not fail loudly — it writes a router whose VRF
# owns nothing, and every interface leaves the routing table. That is the outage
# case, not a no-op, which is why this is probed and not inferred.
#
# What is already known:
#   * the INHERITED router (folder=ngfw-shared) lists $eth-local / $eth-internet,
#     and reports those names even when read at device scope
#   * the INTERFACE objects at device scope are named ethernet1/3 / ethernet1/4
#   * $eth-* are SCM DEFAULTS defined in ngfw-shared and inherited by firewalls;
#     their default_value IS the physical name
# So both are defensible and the answer has to come from SCM.
#
# BLAST RADIUS. The disconnected firewall was deregistered from SCM, so the only
# device scope left is the LIVE one. Two controls make that acceptable:
#   * a NEW router name — this overrides nothing. The inherited `default` router,
#     which every packet traverses, is untouched.
#   * SCM ONLY, never pushed. Config reaches a firewall on push; this probe does
#     not push, so the running config cannot change.
# Destroy immediately after readback.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = { source = "PaloAltoNetworks/scm", version = "~> 1.0" }
  }
}

provider "scm" {}

variable "device" {
  type    = string
  default = "007955000894453"
}

variable "router_name" {
  type        = string
  default     = "fwgitops-naming-probe"
  description = "MUST NOT be `default` — that would override the live router."
  validation {
    condition     = var.router_name != "default"
    error_message = "Refusing to override the inherited `default` router."
  }
}

# Which naming to test. Flip with -var to try the other.
variable "interface_names" {
  type    = list(string)
  default = ["$eth-local"]
}

resource "scm_logical_router" "probe" {
  name   = var.router_name
  device = var.device

  vrf = [{
    name      = "default"
    interface = var.interface_names
  }]
}

output "stored_vrf" { value = scm_logical_router.probe.vrf }
