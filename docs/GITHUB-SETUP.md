# GitHub setup — track C

State of the GitHub side of the pipeline. Repo: **github.com/martono25/palo-firewall-gitops** (private).

## Finding #15 — free-private cannot enforce the approval gate

On the **free** plan, a **private** repo cannot enforce branch protection, rulesets,
or environment **required reviewers** (`403: Upgrade to GitHub Pro or make this
repository public`). The human-approval gate in `apply.yml` (the `firewall-apply`
environment + required review) is therefore **configured but NOT enforced** until
you upgrade to Pro or make the repo public. Verified live 2026-07-23.

Decision: proceed for the solo test phase; enable enforcement for production.

## Done

- [x] Private repo created + pushed.
- [x] `firewall-apply` environment created (id 18599079822). Exists so the workflow
      reference resolves; **no blocking reviewer** (plan-gated).
- [x] `CODEOWNERS` set to `@martono25` (advisory until branch protection enforces it).

## You run (secrets — values never leave your machine)

`gh secret set` reads the value from your input; it is stored encrypted by GitHub
and never printed. Run each and paste the value at the prompt:

```bash
gh secret set SCM_CLIENT_ID      # GitOps@1198884949.iam.panserviceaccount.com
gh secret set SCM_CLIENT_SECRET  # the service-account secret
gh secret set SCM_SCOPE          # tsg_id:1198884949
```

Verify names (not values) landed:

```bash
gh secret list
```

Cloud backend creds (AWS/GCP OIDC for the Terraform state backend, Arch-2) come
later, when the backend is chosen — prefer OIDC over long-lived keys.

## Deferred — enable for production (after Pro or public)

**1. Branch protection on `main`** (require PR + the `pr-validate` check + 1 review):

```bash
gh api --method PUT repos/martono25/palo-firewall-gitops/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["compile-and-plan"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 1, "require_code_owner_reviews": true},
  "restrictions": null
}
JSON
```

**2. Environment required reviewer** (the apply gate):

```bash
REVIEWER_ID=$(gh api user --jq .id)
gh api --method PUT repos/martono25/palo-firewall-gitops/environments/firewall-apply \
  --input - <<JSON
{"reviewers":[{"type":"User","id":$REVIEWER_ID}],"deployment_branch_policy":null}
JSON
```

**3. Real team handles** in `.github/CODEOWNERS` (replace `@martono25`).

Until then: the workflows run, the environment shows deployments, but nothing
BLOCKS an apply. Fine for solo testing; not production-safe.
