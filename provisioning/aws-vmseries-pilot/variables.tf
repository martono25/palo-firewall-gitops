variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project" {
  description = "Name prefix / tag for all resources."
  type        = string
  default     = "fwgitops-pilot"
}

# ── Firewall image + access (prerequisites you provide) ───────────────────
variable "vmseries_ami_id" {
  description = <<-EOT
    The VM-Series BYOL AMI id for this region, from your AWS Marketplace
    subscription page (Marketplace -> your subscriptions -> VM-Series BYOL ->
    launch -> the AMI id shown for ap-southeast-1). Supplying it directly avoids
    product-code guessing. Leave null to let the module look up its default
    version (may be PAYG — prefer setting this explicitly for BYOL).
  EOT
  type        = string
  default     = null
}

variable "instance_type" {
  description = "VM-Series supported instance type (min m5.xlarge = 4 vCPU)."
  type        = string
  default     = "m5.xlarge"

  # SIZED FOR ENIs, NOT FOR CPU. On AWS a VM-Series interface exists only if an
  # ENI sits at the matching device index, and the ENI limit scales with
  # instance size, not with vCPU:
  #
  #   m5.xlarge   4 vCPU   4 ENIs   -> ethernet1/1 .. 1/3 at most
  #   m5.2xlarge  8 vCPU   4 ENIs   -> no gain at all
  #   m5.4xlarge 16 vCPU   8 ENIs   -> ethernet1/4 reachable
  #
  # No 4-vCPU type in ap-southeast-1 offers more than 4 ENIs, so backing
  # ethernet1/4 costs a 16-vCPU instance. The licence is `VM-SERIES-4`
  # (vm-cpu-count: 4), so PAN-OS uses 4 cores whatever the instance provides —
  # the extra 12 vCPU are paid for and idle. That is the deliberate trade, not
  # an oversight: the purchase is ENI slots.
}

variable "ssh_key_name" {
  description = "Name of an EXISTING EC2 key pair in this region (for console/SSH access)."
  type        = string
}

variable "mgmt_allowed_cidr" {
  description = "CIDR allowed to reach the firewall mgmt interface (HTTPS/SSH). Lock to your IP."
  type        = string
  # no default — you must set this so mgmt is not world-open

  # "No default" was not enough: the pilot ran with 0.0.0.0/0 anyway, and on
  # 2026-08-03 the firewall logged 106 failed `admin` logins brute-forced from
  # 147.185.135.0/24. A comment cannot stop that; a validation can.
  #
  # This is the MANAGEMENT plane of a firewall — the interface that owns the
  # device. There is no lab justification for exposing it to the internet, so
  # the escape hatch is deliberately absent: narrow the CIDR, or use a bastion.
  validation {
    condition     = !contains(["0.0.0.0/0", "::/0"], var.mgmt_allowed_cidr)
    error_message = "Refusing to expose the firewall management plane to the internet. Set mgmt_allowed_cidr to your egress IP (e.g. 203.0.113.4/32)."
  }
  validation {
    condition     = can(cidrhost(var.mgmt_allowed_cidr, 0))
    error_message = "mgmt_allowed_cidr must be a valid CIDR, e.g. 203.0.113.4/32."
  }
}

variable "untrust_subnet_cidr" {
  description = "Subnet for ethernet1/3 ($eth-internet). Internet-facing."
  type        = string
  default     = "10.100.2.0/24"
}

variable "trust_subnet_cidr" {
  description = "Subnet for ethernet1/4 ($eth-local). Internal; no default route."
  type        = string
  default     = "10.100.3.0/24"
}

# ── SCM onboarding (prerequisites you provide) ────────────────────────────
variable "scm_folder" {
  description = "Target SCM folder (init-cfg dgname). The device lands here."
  type        = string
  default     = "GitOps"
}

variable "scm_registration_pin_id" {
  description = "Device-certificate registration PIN ID from the Customer Support Portal (CSP) -> Products -> Device Certificates -> Generate Registration PIN. Time-limited."
  type        = string
  sensitive   = true
}

variable "scm_registration_pin_value" {
  description = "Device-certificate registration PIN VALUE from CSP (same page as the PIN id). Time-limited."
  type        = string
  sensitive   = true
}

variable "vmseries_authcode" {
  description = "BYOL VM-Series auth code (goes in the bootstrap package /license/authcodes)."
  type        = string
  sensitive   = true
}

# ── Network ───────────────────────────────────────────────────────────────
variable "vpc_cidr" {
  type    = string
  default = "10.100.0.0/16"
}

variable "mgmt_subnet_cidr" {
  type    = string
  default = "10.100.0.0/24"
}

variable "dataplane_subnet_cidr" {
  type    = string
  default = "10.100.1.0/24"
}
