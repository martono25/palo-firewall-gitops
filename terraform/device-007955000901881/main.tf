# Per-scope root module for FIREWALL 007955000901881. One root == one
# state (design Arch-2).
#
# SEPARATE FROM ITS FOLDER'S ROOT on purpose: a device-scope write does
# not EDIT the inherited object, it creates a per-device OVERRIDE with
# its own id (spike/device-override-probe). Two objects means two
# states; sharing a root would let one scope's plan destroy the
# other's overrides.

terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "1.0.12-beta.4" # matches modules/security_folder/versions.tf
    }
  }
}

provider "scm" {
  # Auth comes from SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE in the
  # environment — the same names `fwgitops` uses. An empty block is
  # correct here, not a TODO.
}

module "security_folder" {
  source = "../modules/security_folder"

  # EVERY module variable is wired. A declared-but-unwired variable is
  # HOLE 2: Terraform emits no diagnostic at all and the data simply
  # never reaches the resource.
  address_objects   = var.address_objects
  folder            = var.folder
  folder_interfaces = var.folder_interfaces
  interfaces        = var.interfaces
  routers           = var.routers
  security_rules    = var.security_rules
  service_objects   = var.service_objects
  zones             = var.zones
}
