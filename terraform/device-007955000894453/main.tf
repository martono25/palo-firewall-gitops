# Per-scope root module for FIREWALL 007955000894453 (fw-prod-edge-4453, PA-VM).
# One root == one state (design Arch-2). `terraform plan` here is the PR preview
# + drift detector.
#
# WHY A SEPARATE ROOT FROM prod-edge. In SCM the firewall is the last level of
# the hierarchy and inherits from `prod-edge`, but a device-scope write does not
# EDIT the inherited object — it creates a per-device OVERRIDE, a distinct object
# with its own id, leaving the shared object and the other firewall untouched
# (verified in spike/device-override-probe). Two different objects means two
# different states; sharing a root would let one scope's plan destroy the
# other's overrides.
#
# The compiler emits here on its own: a `device:` intent compiles to
# terraform/device-<serial>/ (Scope.dirname). Nothing routes by hand.

terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0" # resolved 1.0.11 in the Part-A spike
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

  folder          = var.folder
  address_objects = var.address_objects
  service_objects = var.service_objects
  security_rules  = var.security_rules
  zones           = var.zones
  interfaces      = var.interfaces
  routers         = var.routers
}
