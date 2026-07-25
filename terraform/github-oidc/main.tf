# GitHub Actions OIDC → AWS role for the CI pipeline's Terraform state backend.
# Run once (local state, like bootstrap-backend). Outputs a role ARN to set as
# the AWS_OIDC_ROLE_ARN repo variable. No long-lived AWS keys in GitHub.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  state_bucket = "fw-gitops-tfstate-${data.aws_caller_identity.current.account_id}"
}

# Account-global OIDC provider for GitHub Actions. If it already exists in this
# account, import it instead of creating a second one:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"] # GitHub OIDC; AWS validates the cert, thumbprint is legacy
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Scope to THIS repo (any ref/environment). Two subject forms are accepted so
    # trust holds whether or not the org/repo enables GitHub's immutable numeric
    # ID subject claim (which sends repo:<owner>@<id>/<repo>@<id>:...). Confirmed
    # via the OIDC-claim diagnostic on 2026-07-25. Tighten for production, e.g.
    # ...:environment:firewall-apply for the apply path.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = concat(
        ["repo:${var.repo}:*"],
        var.repo_id_subject != "" ? ["${var.repo_id_subject}:*"] : [],
      )
    }
  }
}

resource "aws_iam_role" "ci" {
  name_prefix        = "fwgitops-ci-"
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = { Project = "palo-firewall-gitops" }
}

# Least privilege: only the Terraform state bucket (objects + native lockfile).
data "aws_iam_policy_document" "state_access" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.state_bucket}"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "state_access" {
  name_prefix = "tfstate-"
  role        = aws_iam_role.ci.id
  policy      = data.aws_iam_policy_document.state_access.json
}
