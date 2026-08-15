# Per-folder root module for SCM folder "GitOps". One root ==
# one state (design Arch-2). `terraform plan` here is the PR preview
# + drift detector.

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
  folder            = var.folder
  folder_interfaces = var.folder_interfaces
  interfaces        = var.interfaces
  routers           = var.routers
  security_rules    = var.security_rules
  zones             = var.zones
}
