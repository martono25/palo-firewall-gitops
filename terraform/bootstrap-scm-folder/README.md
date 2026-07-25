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
# SCM auth in the env (secret via read -rs; id/scope are not secret):
read -rs "SCM_CLIENT_SECRET?SCM client secret: "; echo
export SCM_CLIENT_SECRET
export SCM_CLIENT_ID='GitOps@1198884949.iam.panserviceaccount.com'  # your service account
export SCM_SCOPE='tsg_id:1198884949'

terraform -chdir=terraform/bootstrap-scm-folder init
terraform -chdir=terraform/bootstrap-scm-folder apply
```

Then set the pilot's `scm_folder` to this folder name and provision.
