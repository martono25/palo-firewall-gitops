# CONTROL for the zone rejection in main.tf.
#
# WHY IT IS NOT OPTIONAL. On its own, "device-scope zone create was refused"
# has two explanations that imply completely different builds:
#
#   (a) SCM rejects DEVICE SCOPE on this firewall  -> nothing can be device-scoped
#   (b) SCM rejects device scope FOR ZONES         -> only zones are affected
#
# Nothing in the error tells them apart. It says "Device ... doesn't exist",
# which is wrong under BOTH readings — the device plainly exists, it is
# connected, and GET returns its inherited config.
#
# Same device, same provider, same credentials, different RESOURCE.
#
# INERT BY CONSTRUCTION: ethernet1/2 has an ENI attached (device index 2,
# 10.100.1.110) but no PAN-OS config and no zone membership, so configuring it
# cannot move traffic. MTU only, no `ip` — the same restraint
# spike/device-override-probe used, and for the same reason: an interface with
# no address cannot answer for one.
resource "scm_ethernet_interface" "control" {
  name   = "ethernet1/2"
  device = var.device

  layer3 = {
    mtu = 1476 # distinctive: 1500 - 24
  }
}

output "control_interface_id" {
  value       = scm_ethernet_interface.control.id
  description = "Non-null means device scope works on this firewall, so the zone rejection is resource-specific."
}
