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
      version = "1.0.12-beta.4" # PRE-RELEASE, pinned exactly
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

# ── INTERFACE VARIABLES LIVE IN THE FOLDER'S OWN ROOT, NOT HERE ───────────
# `$eth-dmz` was declared here briefly (v1.18.0) and MOVED OUT in v1.19.0, by
# `state rm` + `import` rather than destroy-and-recreate, because a live zone
# binds it.
#
# The reason is cadence, not ownership. This root is run-once and holds LOCAL,
# gitignored state, so its state exists on exactly one machine. Adding an
# interface is infrequent but ONGOING — filing it here made every later addition
# a manual apply from that one machine, outside the pipeline: no PR plan, no risk
# classification, no evidence bundle, and invisible to drift.
#
# They are now materialised from `catalog/interfaces.yaml` by
# `fwgitops folder-interfaces` into the folder's CI-owned root, sharing the
# remote state its zones and rules already use. See ADR-0005.
#
# WHAT STAYS HERE is only what must exist BEFORE the pipeline can run at all:
# the folder itself, which a firewall names as `dgname` when it registers.
