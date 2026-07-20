# Part-B smoke test — exercises the REAL module against your lab tenant.
#
# Deliberately calls terraform/modules/security_folder (not a parallel config)
# so this validates the exact code that will run in production.
#
# SAFETY: the rule is created with `disabled = true`, so even if a device were
# attached to the folder it cannot affect traffic. Names are prefixed and the
# whole thing is `terraform destroy`-able. Use a LAB tenant + greenfield folder.

terraform {
  required_version = ">= 1.6"

  required_providers {
    scm = {
      source  = "PaloAltoNetworks/scm"
      version = "~> 1.0"
    }
  }
}

provider "scm" {
  # Credentials come from the ENVIRONMENT — never hardcode, never commit:
  #   export SCM_CLIENT_ID=...
  #   export SCM_CLIENT_SECRET=...
  #   export SCM_SCOPE=...            # your TSG / client scope
  # Optional (sane defaults): SCM_AUTH_URL, SCM_HOST, SCM_PROTOCOL, SCM_PORT
  #
  # The provider performs the OAuth exchange itself (client credentials → JWT),
  # which is the native "short-lived token" behavior T1 relies on.
}

locals {
  addr_name = "${var.name_prefix}-addr"
  svc_name  = "${var.name_prefix}-svc"
  rule_name = "${var.name_prefix}-rule"

  # The gitops-managed marker from fwgitops.tags — this is also how we learn
  # whether SCM accepts free-form tag strings or requires scm_tag objects.
  tags = ["gitops:managed"]
}

module "smoke" {
  source = "../../terraform/modules/security_folder"

  address_objects = {
    (local.addr_name) = {
      name   = local.addr_name
      type   = "ip-netmask"
      value  = var.test_cidr
      folder = var.folder
      tags   = local.tags
    }
  }

  service_objects = {
    (local.svc_name) = {
      name     = local.svc_name
      protocol = "tcp"
      port     = var.test_port
      folder   = var.folder
      tags     = local.tags
    }
  }

  security_rules = {
    (local.rule_name) = {
      name         = local.rule_name
      folder       = var.folder
      from_zones   = var.from_zones
      to_zones     = var.to_zones
      sources      = [local.addr_name]
      destinations = [local.addr_name]
      services     = [local.svc_name]
      action       = "allow"
      log_end      = true
      disabled     = true # SAFETY — created disabled
      tags         = local.tags
    }
  }
}

output "created" {
  description = "What the smoke test created (delete with terraform destroy)."
  value = {
    folder  = var.folder
    address = local.addr_name
    service = local.svc_name
    rule    = "${local.rule_name} (disabled)"
  }
}
