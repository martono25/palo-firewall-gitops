# Part-B smoke test

Creates one address object, one service, and one **disabled** rule in your lab
folder — by calling the **real** `security_folder` module, so this validates the
exact code that ships.

## Run

```bash
export SCM_CLIENT_ID='...'
export SCM_CLIENT_SECRET='...'
export SCM_SCOPE='...'            # your TSG / client scope

cd spike/smoke
terraform init
terraform plan  -var 'folder=YOUR_LAB_FOLDER'    # review first
terraform apply -var 'folder=YOUR_LAB_FOLDER'
```

Never put credentials in a `.tf` or `.tfvars` file — the provider reads them
from the environment and does the OAuth exchange itself.

## What to observe (the four Part-B answers)

1. **Commit/push model** — after `apply` succeeds, check SCM: is the config
   *live*, or sitting as a **candidate / uncommitted** change awaiting a push?
   → If a separate push is required, that is our atomic commit boundary and the
   `SCM commit/push` step in `.github/workflows/apply.yml` becomes real.
2. **Tag behavior** — did `gitops:managed` attach as a free-form tag, or did SCM
   reject it because no matching `scm_tag` object exists?
   → If rejected, the module needs an `scm_tag` `for_each` over distinct tags,
   and `fwgitops.tags` may need a stricter charset.
3. **Auth end-to-end** — did the client-credentials → JWT exchange work with the
   scoped service account? (feeds T1)
4. **Ordering** — did the address/service objects get created before the rule
   referenced them (`depends_on`), with no ordering error?

Also worth noting: whether object **names** are accepted as-is (our
`addr-<hash>` / `svc-<hash>` deterministic naming) and whether the folder scope
behaved as expected.

## Clean up (do this)

```bash
terraform destroy -var 'folder=YOUR_LAB_FOLDER'
```

Leaving the objects behind pollutes the folder and will show up later as drift.

## Record the results

Update `terraform/modules/security_folder/README.md` (the "Still open — Part B"
section) and `docs/SPIKE-scm.md` deliverables with what you find.
