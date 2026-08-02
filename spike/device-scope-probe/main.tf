# Does `device=` scope work as an isolated target, and does the provider write
# faithfully there?
#
# WHY THIS EXISTS. v1.11.0 assumed SCM creates a folder per device and shipped
# that into a catalog and an ADR. It does not — `folder=<serial>` returns 400
# "Folder doesn't exist"; the serial is a `device=` scope (ADR-0006 correction).
# Targeting ONE firewall therefore needs a device scope, and before building
# that we probe it, exactly as we probed the provider for rules / zones /
# interfaces / routers (four for four at catching what inference got wrong).
#
# WHAT THIS PROBE DOES *NOT* ANSWER. Device scope projects the SAME object ids
# as the shared folder-scope interfaces:
#
#   folder=ngfw-shared  $eth-internet (default_value: ethernet1/3)  7ff5e3ec-…
#   device=<serial>     ethernet1/3                                 7ff5e3ec-…
#
# So writing an EXISTING interface at device scope might create a per-device
# override or might mutate the object every device inherits. That is the
# question that matters for InterfaceRequest, and it is deliberately NOT probed
# here — answering it means touching an object prod-edge inherits.
#
# This probe uses a NEW name instead, so it touches none of the shared objects.
#
# BLAST RADIUS.
#   * target is the DISCONNECTED firewall (007955000893662) — SCM config only
#     reaches a device on push, and a disconnected device cannot be pushed to
#   * `ethernet1/5` is NOT one of the two interfaces this tenant uses
#     ($eth-internet -> ethernet1/3, $eth-local -> ethernet1/4), so nothing
#     existing is overridden or shadowed
#   * destroyed immediately after readback
# Do NOT point this at the connected firewall.

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
  default     = "ethernet1/5"
  description = "A name this tenant does not use. NOT ethernet1/3 or ethernet1/4."
  validation {
    condition     = !contains(["ethernet1/3", "ethernet1/4", "$eth-local", "$eth-internet"], var.name)
    error_message = "Refusing to touch an interface this tenant actually uses."
  }
}

resource "scm_ethernet_interface" "probe" {
  name = var.name

  # The point of the probe: `device` INSTEAD of `folder`. The provider states
  # "exactly one of device, folder, snippet" on every resource.
  device = var.device

  comment = "fwgitops device-scope probe — safe to delete"

  layer3 = {
    mtu = 1500
    ip  = [{ name = "10.99.98.1/30" }]
  }
}

output "probe_id" { value = scm_ethernet_interface.probe.id }

output "what_terraform_thinks_it_wrote" {
  description = "Compare against readback.py. Divergence == the provider dropped it."
  value = {
    name   = scm_ethernet_interface.probe.name
    device = scm_ethernet_interface.probe.device
    folder = scm_ethernet_interface.probe.folder
    layer3 = scm_ethernet_interface.probe.layer3
  }
}
