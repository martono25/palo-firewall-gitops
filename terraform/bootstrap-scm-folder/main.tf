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

# ── INTERFACE VARIABLES ARE PLATFORM CONFIG, NOT INTENT ───────────────────
# A folder-scope zone can ONLY bind an interface object that exists at that
# scope, and on this tenant those are `$`-prefixed variables. Probed live
# (spike/zone-device-scope), binding a literal port name is refused:
#
#   zone -> network -> layer3 'ethernet1/2' is not a valid reference
#
# so a zone cannot reach a port that has no variable in front of it. SCM fails
# closed here, which is the good outcome.
#
# `$eth-local` and `$eth-internet` came free — they are SCM defaults defined in
# `ngfw-shared` and inherited (catalog/interfaces.yaml). A THIRD role has no
# such default, and nothing in the Day-1 kinds creates one: ADR-0005 has
# `InterfaceRequest` CONFIGURE an interface that already exists, deliberately,
# because creating ports is not what a change request should do.
#
# So the variable is declared HERE, with the folder, for the same reason the
# folder is: it must exist before any intent can reference it, and it is
# platform topology rather than a request. THIS IS A REAL BOUNDARY, not an
# oversight — see the note in TODOS about what it means for a greenfield folder.
#
# Scope is `prod-edge`, NOT `ngfw-shared`. Both would work, since the firewall
# inherits down the whole chain, but `ngfw-shared` also feeds the `GitOps`
# sandbox and every future sibling. Declare at the narrowest scope that reaches
# the target.
resource "scm_ethernet_interface" "eth_dmz" {
  name          = "$eth-dmz"
  folder        = scm_folder.this.name
  default_value = "ethernet1/2"

  # NO addressing here. The IP belongs to an InterfaceRequest at DEVICE scope,
  # exactly as REQ-2026-0801/0802 do it: the address is a property of one
  # firewall's wiring (it must match that firewall's ENI), while the variable is
  # shared. Putting an address on the shared object would give every firewall
  # inheriting this folder the same IP.
  layer3 = {}
}

output "eth_dmz_name" {
  description = "Interface variable a ZoneRequest can bind. Mirrors catalog/interfaces.yaml role `dmz`."
  value       = scm_ethernet_interface.eth_dmz.name
}
