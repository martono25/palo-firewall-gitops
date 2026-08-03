# The VM-Series firewall itself — Palo's maintained module (2.2.7). It creates the
# ENIs from `interfaces`, allocates the mgmt EIP (create_public_ip), and launches
# the instance with our BYOL AMI + bootstrap package + instance profile.
#
# No mgmt-interface-swap: eth0 (device_index 0) stays management, in the public
# subnet with a public IP, so it can reach SCM and the S3 bootstrap bucket.

module "vmseries" {
  source  = "PaloAltoNetworks/swfw-modules/aws//modules/vmseries"
  version = "2.2.7"

  name                 = var.project
  vmseries_ami_id      = var.vmseries_ami_id
  instance_type        = var.instance_type
  ssh_key_name         = var.ssh_key_name
  iam_instance_profile = aws_iam_instance_profile.bootstrap.name

  interfaces = {
    mgmt = {
      device_index       = 0
      subnet_id          = aws_subnet.mgmt.id
      security_group_ids = [aws_security_group.mgmt.id]
      create_public_ip   = true
    }
    dataplane = {
      device_index       = 1
      subnet_id          = aws_subnet.dataplane.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
    # ── ethernet1/2 .. ethernet1/4 ────────────────────────────────────────
    # device_index N maps to ethernet1/N. The SCM folder variables already point
    # $eth-internet -> ethernet1/3 and $eth-local -> ethernet1/4, so those two
    # indexes are what make the existing config real; before this they had
    # placeholder MACs and were link-down.
    #
    # source_dest_check MUST stay false on every dataplane ENI — a firewall
    # forwards traffic it did not originate, and AWS drops that otherwise.
    spare = {
      # Index 2 exists only to keep the indexes contiguous (see network.tf).
      # Unconfigured on the firewall.
      device_index       = 2
      subnet_id          = aws_subnet.dataplane.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
    untrust = {
      # ethernet1/3 = $eth-internet
      device_index       = 3
      subnet_id          = aws_subnet.untrust.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
    trust = {
      # ethernet1/4 = $eth-local — the interface REQ-2026-0801 addresses.
      device_index       = 4
      subnet_id          = aws_subnet.trust.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
  }

  # Point the firewall at the S3 bootstrap package (read via the instance profile).
  bootstrap_options = "vmseries-bootstrap-aws-s3bucket=${aws_s3_bucket.bootstrap.id}"

  tags = { Project = var.project }
}
