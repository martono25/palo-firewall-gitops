# spike/ — scm provider spike toolkit

Supports [`docs/SPIKE-scm.md`](../docs/SPIKE-scm.md). Prereqs (tenant, service
account, greenfield folder) are done; this is how you run it.

## Part A — schema dump (NO credentials, NO tenant needed)

```bash
cd spike/schema-probe
terraform init                                    # note the resolved provider version
terraform providers schema -json > ../schema.json
cd ../..
./spike/schema-answers.sh spike/schema.json
```

`schema-probe/` is isolated on purpose: no version constraint (so a wrong pin
can't fail `init`), no backend, no provider config. Part A only downloads the
provider and reads its schema.

`schema-answers.sh` prints an answer sheet mapped 1:1 to the Part-A checklist:
provider auth attributes, real resource names, address scope/type/tag attrs,
service protocol block shape, and rule member/application/logging attrs.

**Then:** paste the answer sheet (and commit `spike/schema.json` — it's
deliverable #1) so the module's `# VERIFY:` markers can be resolved and the
Part-B smoke config written against the real schema.

## Part B — live smoke test (needs the tenant + folder)

Written *after* Part A, so it targets the real schema rather than guesses.
It creates one address object, one service, and one rule in the greenfield
folder to answer:

- the **commit/push model** (does `apply` push, or is a separate commit needed?)
  — this wires the atomic step in `.github/workflows/apply.yml`
- **auth end-to-end** (feeds T1)
- **tag behavior** on a real object
- whether `depends_on` gives sufficient objects-before-rules ordering

## Note on the version pin

Record the version `terraform init` resolves, then pin it in:
- `terraform/modules/security_folder/versions.tf`
- `terraform/prod-edge/main.tf`
