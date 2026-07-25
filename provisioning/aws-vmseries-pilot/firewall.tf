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
  }

  # Point the firewall at the S3 bootstrap package (read via the instance profile).
  bootstrap_options = "vmseries-bootstrap-aws-s3bucket=${aws_s3_bucket.bootstrap.id}"

  tags = { Project = var.project }
}
