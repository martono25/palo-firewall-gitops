# Remote state — ONE state per folder (design Arch-2: cloud object store + native
# locking, encrypted at rest, never committed to Git). Uncomment and fill in
# during setup; pick the backend matching the cloud you provision the pilot in.
#
# AWS (S3):
# terraform {
#   backend "s3" {
#     bucket       = "REPLACE-fw-gitops-tfstate"
#     key          = "prod-edge/terraform.tfstate"
#     region       = "REPLACE"
#     encrypt      = true
#     use_lockfile = true   # native S3 state locking (Terraform >= 1.10)
#     #                       # older TF: set dynamodb_table = "REPLACE-tf-locks"
#   }
# }
#
# GCP (GCS):
# terraform {
#   backend "gcs" {
#     bucket = "REPLACE-fw-gitops-tfstate"   # enable object versioning on the bucket
#     prefix = "prod-edge"
#   }
# }
