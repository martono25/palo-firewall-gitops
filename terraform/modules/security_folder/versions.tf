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
      source  = "PaloAltoNetworks/scm"
      # PINNED EXACTLY: 1.0.12-beta.4 is a PRE-RELEASE. `~> 1.0` would not even
      # select it (Terraform excludes pre-releases from range constraints), and a
      # floating constraint would drift off it in either direction without anyone
      # choosing to. It is adopted deliberately -- it writes the security-rule
      # fields 1.0.11 silently drops (ADR-0003 addendum).
      version = "1.0.12-beta.4"
    }
  }
}
