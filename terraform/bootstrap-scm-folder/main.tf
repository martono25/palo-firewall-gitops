# Run-once SCM bootstrap: create the configuration folder that the per-change
# GitOps root (terraform/prod-edge) manages objects INSIDE, and that the
# VM-Series associates to at registration (init-cfg dgname).
#
# WHY A BOOTSTRAP (not part of terraform/prod-edge): the folder must exist
# BEFORE the firewall boots and auto-registers with dgname=<folder>. apply.yml
# runs only after provisioning, so it cannot be what creates the folder — same
# ordering reason bootstrap-backend and github-oidc are separate run-once roots.
# Local state, run manually once.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0" # 1.0.11 in the Part-A spike
    }
  }
}

# Auth from SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE (env) — never hardcoded.
provider "scm" {}

resource "scm_folder" "this" {
  name        = var.folder_name
  parent      = var.parent_folder
  description = var.description

  # The provider returns [] (not null) for these optional lists after apply;
  # declaring them empty avoids the "inconsistent result after apply" bug
  # (null -> cty.ListValEmpty) on scm_folder create.
  labels   = []
  snippets = []
}
