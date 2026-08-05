# CONTROL — without it the probe result is an anecdote.
#
# "Device-scope rule rejected" has two readings:
#
#   (a) SCM refuses DEVICE SCOPE on this firewall   -> nothing can be device-scoped
#   (b) SCM refuses device scope FOR RULES          -> only rules are affected
#
# They imply different builds, and the error message cannot tell them apart: it
# says "Device <serial> doesn't exist", which is wrong under both readings —
# the device exists, is connected, and GET returns its inherited config.
#
# Same firewall, same provider, same credentials, same apply, different RESOURCE.
#
# INERT BY CONSTRUCTION, and ethernet1/1 is chosen deliberately:
#   * it is NOT managed by terraform/device-007955000894453, which owns
#     ethernet1/2, ethernet1/3 and ethernet1/4 — so this local state cannot
#     collide with the S3-backed one over the same object
#   * it has an ENI behind it (device index 1, 10.100.1.37) but no PAN-OS config
#     and no zone, so configuring it cannot move traffic
#   * MTU ONLY, no `ip`. An interface with no address cannot answer for one —
#     the same restraint spike/device-override-probe used.
# The real root is re-planned after destroy to prove it was untouched.
resource "scm_ethernet_interface" "control" {
  name   = "ethernet1/1"
  device = var.device

  layer3 = {
    mtu = 1476 # distinctive: 1500 - 24
  }
}

output "control_interface_id" {
  value       = scm_ethernet_interface.control.id
  description = "Non-null means device scope works on this firewall, so a rule rejection is resource-specific."
}
