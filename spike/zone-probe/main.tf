# T5 — scm_zone field-fidelity probe.
#
# Question: does paloaltonetworks/scm v1.0.11 actually WRITE the zone security
# fields, or silently drop them the way it drops profile_setting / log_setting /
# ordering on scm_security_rule (ADR-0003, verified live 2026-07-28)?
#
# Deliberately uses LOCAL state and an EMPTY layer3 interface list:
#   * local state  -> cannot disturb the S3-backed prod-edge state
#   * no interfaces -> the zone carries no traffic, so a botched probe is inert
#
# PHASE 1 (no references needed): the boolean/top-level fields.
# PHASE 2 (needs real object names from discover.py): the referenced fields.

terraform {
  required_version = ">= 1.6"
  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0" # pin to the version in production use
    }
  }
}

# Auth comes from the environment (SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE),
# same contract as src/fwgitops/scmapi.py ScmCredentials.from_env.
provider "scm" {}

variable "folder" {
  type        = string
  description = "SCM folder to create the probe zone in. Use a scratch folder."

  # Prose in a README is not a guard. interface-probe enforces this; zone-probe
  # did not, which is an inconsistency between two spikes doing the same kind of
  # thing with the same credentials.
  #   * prod-edge   — holds the live policy and 2 real devices
  #   * ngfw-shared — parent of prod-edge AND GitOps, so it feeds both
  #   * All         — root of the hierarchy
  validation {
    condition     = !contains(["prod-edge", "ngfw-shared", "All"], var.folder)
    error_message = "Refusing to probe in a folder that feeds production devices. Use GitOps."
  }
}

variable "zone_name" {
  type    = string
  default = "fwgitops-probe-zone"
}

# PHASE 2 inputs. Leave null to run phase 1 only. Populate from discover.py
# output — these MUST be names that already exist on the tenant or SCM rejects
# the create with INVALID_REFERENCE and the probe tells us nothing.
variable "zone_protection_profile" {
  type    = string
  default = null
}

variable "log_setting" {
  type    = string
  default = null
}

resource "scm_zone" "probe" {
  name   = var.zone_name
  folder = var.folder

  # ── Phase 1: top-level booleans, no references required ────────────────
  # enable_user_identification is the one with a proven downstream consequence:
  # the rule model fully supports source_user, but User-ID must be on per-zone
  # or a user-scoped rule silently never matches.
  enable_user_identification   = true
  enable_device_identification = true

  network = {
    layer3 = [] # empty on purpose — inert zone, no traffic

    # ── Phase 2: reference-typed fields ──────────────────────────────────
    zone_protection_profile = var.zone_protection_profile
    log_setting             = var.log_setting
  }
}

output "probe_zone_id" {
  value       = scm_zone.probe.id
  description = "Feed this to readback.py to fetch what SCM actually stored."
}

output "what_terraform_thinks_it_wrote" {
  description = "Compare against readback.py output. Divergence == the provider dropped the field."
  value = {
    name                         = scm_zone.probe.name
    enable_user_identification   = scm_zone.probe.enable_user_identification
    enable_device_identification = scm_zone.probe.enable_device_identification
    network                      = scm_zone.probe.network
  }
}
