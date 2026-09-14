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

  # IMDSv2 REQUIRED (session token; hop limit 1). The module defaults it OFF.
  #
  # The instance role reads the bootstrap bucket, which holds the SCM
  # registration PIN (valid to 2027-03-15 — it outlives any one AWS account) and
  # the BYOL auth code. With IMDSv1, a single forged GET — an SSRF in the mgmt
  # web UI — returns that role's credentials.
  #
  # SUPPORTED, per Palo Alto (read 2026-09-14, not recalled): BYOL VM-Series on
  # PAN-OS 10.2+ with VM-Series plugin 3.0.0+ (KB kA14u000000CqfQCAS). This AMI is
  # 11.2.12. PLUG-10410 — "early licensing ... failing on IMDSv2 ... AWS" — is a
  # known issue in plugin 3.0.2 and absent from the 4.0.2 list; no addressed-issue
  # entry was found. VERIFY LICENSING ON FIRST BOOT. If it fails, relax without a
  # relaunch: `aws ec2 modify-instance-metadata-options --http-tokens optional`.
  enable_imdsv2 = true

  interfaces = {
    mgmt = {
      device_index       = 0
      subnet_id          = aws_subnet.mgmt.id
      security_group_ids = [aws_security_group.mgmt.id]
      create_public_ip   = true
    }

    # ── THREE DATAPLANE ENIs, CONTIGUOUS FROM INDEX 1 ─────────────────────
    # device_index N maps to ethernet1/N, and an ENI at index 4 is what forced
    # a 16-vCPU instance: 4 vCPU caps at 4 ENIs on EVERY family in
    # ap-southeast-1 (checked against `describe-instance-types`, not recalled),
    # so mgmt + 3 dataplane is the ceiling. That is enough — this firewall only
    # ever used three roles.
    #
    # The old layout wasted one: a `spare` ENI at index 2 existed purely to keep
    # indexes contiguous up to 3 and 4, because $eth-internet and $eth-local
    # resolve to ethernet1/3 and ethernet1/4. Those are SCM defaults inherited
    # from `ngfw-shared`; re-pointing them to ethernet1/2 and ethernet1/1 is what
    # makes this layout legal, and it is a change in SCM, not here.
    #
    # DO NOT APPLY THIS BEFORE THAT SCM CHANGE. The zones bind the folder
    # variables, so a firewall built this way with the old defaults has its zones
    # pointing at ports that have no ENI behind them.
    #
    # source_dest_check MUST stay false on every dataplane ENI — a firewall
    # forwards traffic it did not originate, and AWS drops that otherwise.

    trust = {
      # ethernet1/1 = $eth-local — the interface REQ-2026-0801 addresses.
      device_index       = 1
      subnet_id          = aws_subnet.trust.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
    untrust = {
      # ethernet1/2 = $eth-internet
      device_index       = 2
      subnet_id          = aws_subnet.untrust.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
    dmz = {
      # ethernet1/3 = $eth-dmz, created by this platform in `prod-edge`
      # (catalog/interfaces.yaml `create_in`) rather than inherited.
      device_index       = 3
      subnet_id          = aws_subnet.dataplane.id
      security_group_ids = [aws_security_group.dataplane.id]
      source_dest_check  = false
    }
  }

  # Point the firewall at the S3 bootstrap package (read via the instance profile).
  bootstrap_options = "vmseries-bootstrap-aws-s3bucket=${aws_s3_bucket.bootstrap.id}"

  tags = { Project = var.project }
}
