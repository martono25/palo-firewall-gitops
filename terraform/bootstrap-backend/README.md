# bootstrap-backend — create the S3 state bucket (Arch-2, run once)

The bucket that holds Terraform state can't live in that state, so this config
uses LOCAL state and is applied once by hand. It creates an S3 bucket
(versioned, encrypted, TLS-only, no public access) named
`fw-gitops-tfstate-<ACCOUNT_ID>` in ap-southeast-1.

## Prerequisites

- AWS credentials configured (`aws configure` / SSO) with rights to create S3
  buckets. Confirm: `aws sts get-caller-identity`.

## Run once

```bash
cd terraform/bootstrap-backend
terraform init
terraform apply            # review the plan; it creates 1 bucket + settings
terraform output state_bucket
```

Its own state stays local here (`terraform.tfstate`, gitignored). Do NOT delete
that file — it tracks the bucket. (For a team, migrate this bootstrap state into
the bucket afterward; for the pilot, local is fine.)

## Then point the per-folder root at it

```bash
cd ../prod-edge
cp backend.hcl.example backend.hcl        # backend.hcl is gitignored
# edit backend.hcl: set bucket = the output above (fill <ACCOUNT_ID>)
terraform init -backend-config=backend.hcl
```

`terraform plan` there now keeps state in S3 with native locking.

## Cost

Effectively free — an empty versioned S3 bucket costs pennies/month. No compute.
