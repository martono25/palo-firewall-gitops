# bootstrap-scm-folder

Run-once root that creates the SCM configuration folder the GitOps pipeline
targets (`prod-edge` under `ngfw-shared` — the container the SCM UI labels
"All Firewalls"; verified via `/config/setup/v1/folders`, where `GitOps` also
sits under `ngfw-shared`).

**Why separate:** the folder must exist *before* a VM-Series boots and
auto-registers into it (`init-cfg` `dgname=<folder>`). The per-change
`apply.yml` runs only *after* provisioning, so folder creation can't live there —
same ordering reason `bootstrap-backend` and `github-oidc` are their own roots.
Local state; not touched by CI (the plan/apply loops skip `bootstrap-*`).

## Run once

```bash
# SCM auth in the env. ALL THREE are treated as secrets — `.github/scripts/
# redact.py` strips SCM_CLIENT_ID and SCM_SCOPE alongside the secret, and they
# are GitHub secrets, so they do not belong in a file in a public repository.
# This block used to print the real service-account identity and tenant id as
# example values; they had been committed here since 2026-07-23.
read -rs "SCM_CLIENT_SECRET?SCM client secret: "; echo
export SCM_CLIENT_SECRET
read -r  "SCM_CLIENT_ID?SCM client id (svc@<tenant>.iam.panserviceaccount.com): "
export SCM_CLIENT_ID
read -r  "SCM_SCOPE?SCM scope (tsg_id:<tenant>): "
export SCM_SCOPE

terraform -chdir=terraform/bootstrap-scm-folder init
terraform -chdir=terraform/bootstrap-scm-folder apply
```

Then set the pilot's `scm_folder` to this folder name and provision.
