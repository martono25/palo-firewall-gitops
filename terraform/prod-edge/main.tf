# Per-folder root module for SCM folder "prod-edge". One root == one state
# (design Arch-2). `terraform plan` here is the PR preview + drift detector.

terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "1.0.12-beta.4" # PRE-RELEASE, pinned exactly — see below
    }
  }
}

provider "scm" {
  # Auth: a short-lived token minted per CI run (design T1 — single SCM service
  # account, per-run token exchange on a GitHub-hosted runner). Supplied via
  # environment, NEVER hardcoded here.
  #
  # VERIFIED 2026-07-31 against the v1.0.11 provider schema and a live apply:
  # the provider reads SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE from the
  # environment — the same names `fwgitops` uses (see scmapi.ScmCredentials),
  # so one `set -a; source ~/.fwgitops/scm.env` covers both. Optional overrides:
  # SCM_HOST, SCM_AUTH_URL. An empty block is therefore correct, not a TODO.
}

module "security_folder" {
  source = "../modules/security_folder"

  folder            = var.folder
  address_objects   = var.address_objects
  service_objects   = var.service_objects
  security_rules    = var.security_rules
  zones             = var.zones
  interfaces        = var.interfaces
  folder_interfaces = var.folder_interfaces
  routers           = var.routers
}
