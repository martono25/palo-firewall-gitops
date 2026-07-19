# Per-folder root module for SCM folder "prod-edge". One root == one state
# (design Arch-2). `terraform plan` here is the PR preview + drift detector.

terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 0.9" # VERIFY: pin during the spike (match the module)
    }
  }
}

provider "scm" {
  # Auth: a short-lived token minted per CI run (design T1 — single SCM service
  # account, per-run token exchange on a GitHub-hosted runner). Supplied via
  # environment, NEVER hardcoded here.
  #
  # VERIFY: exact provider auth attributes / env vars
  #   (host / auth_url / client_id / client_secret / scope / token).
}

module "security_folder" {
  source = "../modules/security_folder"

  address_objects = var.address_objects
  service_objects = var.service_objects
  security_rules  = var.security_rules
}
