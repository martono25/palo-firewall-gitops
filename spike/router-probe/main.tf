# TODOS P1 gate — does the scm provider WRITE scm_logical_router's nested static
# routes, or silently drop them the way it drops profile_setting / log_setting /
# ordering on scm_security_rule (ADR-0003)?
#
# Fidelity varies PER RESOURCE TYPE and cannot be inferred. The record so far:
#   * scm_security_rule       -> DROPS fields (hence `fwgitops enrich`)
#   * scm_zone                -> faithful   (spike/zone-probe)
#   * scm_ethernet_interface  -> faithful   (spike/interface-probe)
# A static route sits FOUR levels deep (vrf[].routing_table.ip.static_route[]),
# which is the shape most likely to be only partially handled.
#
# WHY THIS MATTERS MORE THAN THE OTHERS. If the provider drops nested routes the
# failure is silent AND convergent: apply succeeds, the router keeps its
# interface membership, no route is written, and Terraform reports no diff
# forever after. Nothing downstream would ever notice.
#
# BLAST RADIUS. Everything here is NEW and lives in `GitOps`:
#   * GitOps has ZERO devices and nothing inherits FROM it -> no device reached
#   * new router name, new interface name -> overrides nothing, shadows nothing
#   * the probe interface is created HERE, so the router claims an interface no
#     real VRF has ever held (an interface belongs to one VRF at a time)
#   * routes point at TEST-NET-1 (192.0.2.0/24, RFC 5737) via a link-local-ish
#     /30 that exists nowhere -- never a default route
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

variable "router_name" {
  type    = string
  default = "fwgitops-probe-router"
}

variable "interface_name" {
  type    = string
  default = "$eth-fwgitops-probe"
}

# The router's VRF must own an interface that exists. Creating it here keeps the
# probe self-contained and means no real interface is ever claimed.
resource "scm_ethernet_interface" "probe" {
  name    = var.interface_name
  folder  = var.folder
  comment = "fwgitops router probe — safe to delete"

  layer3 = {
    mtu = 1500
    ip  = [{ name = "10.99.99.1/30" }]
  }
}

resource "scm_logical_router" "probe" {
  name   = var.router_name
  folder = var.folder

  vrf = [{
    name = "default"

    # Membership. The compiler carries this on every compiled route precisely
    # because Terraform manages whole objects — if the provider drops it, a real
    # apply would evict every interface from the VRF.
    interface = [scm_ethernet_interface.probe.name]

    routing_table = {
      ip = {
        static_route = [
          {
            # Next hop by IP, with both optional metrics set.
            name        = "probe-via-ip"
            destination = "192.0.2.0/24"
            nexthop     = { ip_address = "10.99.99.2" }
            metric      = 17
            admin_dist  = 33
          },
          {
            # Next hop by INTERFACE — the other arm of the compiler's
            # exactly-one-next-hop rule, and a different provider code path.
            name        = "probe-via-interface"
            destination = "198.51.100.0/24"
            interface   = scm_ethernet_interface.probe.name
          },
        ]
      }
    }
  }]
}

output "probe_id" { value = scm_logical_router.probe.id }

output "what_terraform_thinks_it_wrote" {
  description = "Compare against readback.py. Divergence == the provider dropped it."
  value       = scm_logical_router.probe.vrf
}
