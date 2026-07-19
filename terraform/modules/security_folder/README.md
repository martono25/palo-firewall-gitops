# security_folder — static Terraform module

Consumes the compiler's `rules.auto.tfvars.json` and creates the address
objects, service objects, and security rules for one SCM folder via `for_each`.
The module is authored + reviewed once; only the generated data changes per PR.

```
rules.auto.tfvars.json ──▶ root module vars ──▶ module "security_folder" ──▶ scm_* resources
```

## ⚠️ Status: UNVALIDATED against the live provider

The variable types (the compiler contract) are correct and stable. The **`scm`
provider resource/attribute schema is not verified** — no `scm` provider or SCM
credentials are available in the build environment. This module is the concrete
output of, and input to, the **scm provider coverage spike** (the #1 de-risker
in docs/DESIGN.md → The Assignment).

## Spike checklist — resolve every `# VERIFY:` before first apply

Run `terraform providers schema -json` against a pinned `scm` provider and
confirm each item, then delete the corresponding `# VERIFY:` comment:

- [ ] **Provider version** — pin the exact `PaloAltoNetworks/scm` version (`versions.tf`).
- [ ] **Scope attribute** — object/rule scope is `folder` vs `snippet` vs `device`;
      confirm exactly one is required and that `folder` is right for this design.
- [ ] **`scm_address_object`** — attribute for CIDR (`ip_netmask`?) and FQDN (`fqdn`?),
      and that they are mutually exclusive.
- [ ] **Tags** — attribute name (`tags` vs `tag`) and element type on every resource.
      Confirm the tag *strings* our convention emits (`gitops:managed`, …) are legal
      SCM tag names, or whether tags must be pre-created `scm_tag` objects (if so, add
      a `scm_tag` for_each over the distinct tags and a dependency).
- [ ] **`scm_service`** — protocol block shape (`protocol { tcp { port } }`?) and
      whether a port *range* string (`8000-8100`) is accepted as-is.
- [ ] **Security rule resource name** — `scm_security_policy_rule` vs `scm_security_rule`.
- [ ] **Rule member attrs** — `from` / `to` / `source` / `destination` / `service`
      names and whether they take plain string lists or object references.
- [ ] **`application`** — attribute name and the correct "any" literal (Phase-1 default).
- [ ] **Logging** — `log_end` vs `log_setting` / `log_start`, and default.
- [ ] **Commit/push model** — does the provider push config to SCM on `apply`, or is a
      separate commit/push needed? This drives the candidate/commit boundary
      (docs/DESIGN.md → Change Rollback) and the pipeline's atomic step.
- [ ] **Ordering** — confirm `depends_on` (objects before rules) is sufficient, or
      whether the provider needs explicit references for ordering.

## Inputs

| Variable | Source |
|---|---|
| `address_objects` / `service_objects` / `security_rules` | `rules.auto.tfvars.json` (auto-loaded in the per-folder root) |

## Usage

See `terraform/prod-edge/` for the per-folder root that pins the backend,
configures the provider (short-lived token — design T1), and calls this module.
