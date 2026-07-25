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
}

variable "ssh_key_name" {
  description = "Name of an EXISTING EC2 key pair in this region (for console/SSH access)."
  type        = string
}

variable "mgmt_allowed_cidr" {
  description = "CIDR allowed to reach the firewall mgmt interface (HTTPS/SSH). Lock to your IP."
  type        = string
  # no default — you must set this so mgmt is not world-open
}

# ── SCM onboarding (prerequisites you provide) ────────────────────────────
variable "scm_folder" {
  description = "Target SCM folder (init-cfg dgname). The device lands here."
  type        = string
  default     = "GitOps"
}

variable "scm_registration_pin_id" {
  description = "SCM auto-registration PIN id (SCM -> device onboarding). Time-limited."
  type        = string
  sensitive   = true
}

variable "scm_registration_pin_value" {
  description = "SCM auto-registration PIN value. Time-limited."
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
