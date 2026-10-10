# Remote state — ONE state per scope (design Arch-2: S3 + native locking,
# encrypted, never in Git). PARTIAL config: the concrete bucket/region/key live
# in backend.hcl (filled from the bootstrap output), passed at init:
#
#   terraform init -backend-config=backend.hcl
#
# Partial config keeps the bucket name (which contains the account id) out of
# the tracked .tf and lets CI pass the same values.

terraform {
  backend "s3" {}
}
