# VM-Series pilot (Day-1) — AWS, BYOL, onboarding to Strata Cloud Manager.
#
# Standard AWS resources (VPC, S3 bootstrap bucket, IAM) are hand-written here;
# the firewall instance uses Palo's maintained vmseries module (2.2.7). BYOL AMI
# is supplied via var.vmseries_ami_id (from your AWS Marketplace subscription) so
# there is no product-code guessing.
#
# COST: BYOL = EC2 only (~$0.30/hr for m5.xlarge in ap-southeast-1) + EIP. Plan is
# stand up -> onboard -> confirm push -> `terraform destroy`. Do not leave running.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {} # init with -backend-config=backend.hcl (generate via ../../terraform/make-backend.sh)
}

provider "aws" {
  region = var.region
}
