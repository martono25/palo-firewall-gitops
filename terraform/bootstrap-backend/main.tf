# Bootstrap: create the S3 bucket that holds Terraform state (Arch-2).
#
# Chicken-and-egg: the bucket that stores TF state cannot itself live in that
# state. So this config uses LOCAL state and is applied ONCE, by hand. After it
# exists, the per-folder roots (terraform/<folder>/) point their backend at it.
#
# Native S3 state locking (use_lockfile, Terraform >= 1.10) — no DynamoDB table.
# Region: ap-southeast-1 (Singapore).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # LOCAL state on purpose — do not add an S3 backend here.
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  # Deterministic, globally-unique name tied to the account — no random state.
  bucket_name = "fw-gitops-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.bucket_name

  tags = {
    Project   = "palo-firewall-gitops"
    Purpose   = "terraform-state"
    ManagedBy = "bootstrap-backend"
  }
}

# Recover prior state versions if a bad apply corrupts it.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt at rest. SSE-S3 (AES256) for the pilot; production should use SSE-KMS
# with a customer-managed key (state contains rendered firewall config).
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# State is sensitive — never public.
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Refuse any non-TLS request to the bucket.
resource "aws_s3_bucket_policy" "tls_only" {
  bucket = aws_s3_bucket.tfstate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.tfstate.arn,
        "${aws_s3_bucket.tfstate.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })
}
