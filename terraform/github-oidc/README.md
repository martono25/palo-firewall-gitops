# github-oidc — CI role for the Terraform state backend (run once)

Lets GitHub Actions assume an AWS role via OIDC (no long-lived keys) with
least-privilege access to the Terraform state bucket only.

## Run once

```bash
cd terraform/github-oidc
terraform init
terraform apply
terraform output ci_role_arn
```

If the account already has a GitHub OIDC provider, import it first (see the note
in main.tf) so apply doesn't try to create a duplicate.

## Wire it into CI

```bash
gh variable set AWS_OIDC_ROLE_ARN --body "<ci_role_arn from output>"
```

`apply.yml` (and pr-validate once wired) read `${{ vars.AWS_OIDC_ROLE_ARN }}` to
configure AWS credentials for the S3 backend.

## Scope

The trust policy allows `repo:martono25/palo-firewall-gitops:*` (any ref/env).
Tighten for production, e.g. `:environment:firewall-apply` for apply.
