# ─────────────────────────────────────────────────────────────────────────
# UNVALIDATED SCAFFOLDING. This module is structured against the compiler's
# rules.auto.tfvars.json contract (which is verified), but the `scm` provider
# resource/attribute schema below is NOT verified — that is the "scm provider
# coverage spike" (docs/DESIGN.md → The Assignment, the #1 de-risker).
# Every schema assumption is marked `# VERIFY:`. See README.md for the checklist.
# ─────────────────────────────────────────────────────────────────────────
terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source = "PaloAltoNetworks/scm"
      # VERIFY: pin the exact provider version during the spike.
      version = "~> 0.9"
    }
  }
}
