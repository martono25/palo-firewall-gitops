# Source NAT so the east-west traffic harness can complete a round trip.
#
# WHY THIS EXISTS, AND WHY IT IS NOT AN INTENT. `NatRequest` is deferred to v2.0
# and unimplemented, so this cannot go through the compiler. It is TEST-HARNESS
# config, applied deliberately outside the GitOps path, and it is the honest
# place to say so — the security policy under test still comes from an intent
# (REQ-2026-0804); only the plumbing that lets a packet complete does not.
#
# THE PROBLEM IT SOLVES. Without NAT the firewall forwards the client's SYN out
# ethernet1/3 still carrying source 10.100.3.200 — an address belonging to the
# TRUST subnet, emitted into the UNTRUST subnet. Proven with a dataplane capture:
#
#   tx.pcap  10.100.3.200:41190 > 10.100.2.200:80  Flags [S]      (firewall sends)
#   rx.pcap  (nothing from 10.100.2.200, ever)                    (no reply)
#   target   PassiveOpens = 0                                     (never arrived)
#
# So AWS does not deliver it, source/dest check disabled notwithstanding, and
# neither a host route on the target nor a VPC route to the firewall's ENI
# changed that. Both were tried; the VPC route made it worse by pointing the
# source subnet back at the sending ENI.
#
# With source NAT the SYN leaves as 10.100.2.142 — a real address in the target's
# own subnet — so delivery is ordinary, and the reply comes back to the firewall
# without any special routing at either end. This is why AWS VM-Series designs
# NAT east-west traffic rather than relying on symmetric routing.

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
  default = "prod-edge"
}

resource "scm_nat_rule" "harness_snat" {
  name        = "fwgitops-harness-snat"
  folder      = var.folder
  description = "TEST HARNESS, not intent-managed. Source NAT so east-west traffic completes; see spike/traffic-harness-nat."

  from        = ["local"]
  to          = ["internet"]
  source      = ["any"]
  destination = ["any"]
  service     = "any"

  source_translation = {
    dynamic_ip_and_port = {
      interface_address = {
        # Translate to the untrust interface's own address (10.100.2.142).
        interface = "$eth-internet"
      }
    }
  }
}

output "note" {
  value = "Harness NAT applied to ${var.folder}. Destroy with `terraform destroy` when the harness is not in use."
}
