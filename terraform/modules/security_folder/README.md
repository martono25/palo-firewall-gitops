# security_folder — static Terraform module

Consumes the compiler's `rules.auto.tfvars.json` and creates the address
objects, service objects, and security rules for one SCM folder via `for_each`.
The module is authored + reviewed once; only the generated data changes per PR.

```
rules.auto.tfvars.json ──▶ root module vars ──▶ module "security_folder" ──▶ scm_* resources
```

## ✅ Status: schema-VERIFIED (Part A done, 2026-07-19)

Verified against **`PaloAltoNetworks/scm` v1.0.11**. `terraform validate` passes.
Schema dump: [`spike/schema.json`](../../../spike/schema.json).

### Part-A findings (what changed vs the original guesses)

| Assumed | Actual | Impact |
|---|---|---|
| `scm_address_object` | **`scm_address`** | resource renamed |
| `scm_security_policy_rule` | **`scm_security_rule`** | resource renamed |
| `tags` | **`tag`** (singular, `list(string)`) | all three resources |
| `protocol { tcp { port } }` block | **nested ATTRIBUTE**: `protocol = { tcp = { port } }` | `dynamic` block would have failed |
| provider `~> 0.9` | **1.0.11** (`~> 1.0`) | wrong pin would have failed `init` |

Confirmed as designed: scope is exactly one of `folder`/`snippet`/`device`;
address type is exactly one of `ip_netmask`/`fqdn`/`ip_range`/`ip_wildcard`;
rule members (`from`/`to`/`source`/`destination`/`service`/`application`) are all
`list(string)`; `action` is a string; `log_end` is a bool.

Useful extras discovered: the rule also exposes `position` / `relative_position`
(relevant to the Phase-2 sectioned-placement design), plus `disabled`,
`negate_source`/`negate_destination`, `source_user`, `schedule`, and
`profile_setting`. There are 129 `scm_*` resources total, including `scm_folder`,
`scm_snippet`, `scm_label`, `scm_variable`, and `scm_zone` (useful for Day-1).

## ✅ Part B done — live apply against a lab tenant (2026-07-19)

- [x] **Commit/push model** — the provider has **no push/commit capability** (0/129
      resources, 0/252 data sources). `apply` only **stages** config; a separate push
      (**target = folder**) makes it live. The candidate/commit boundary is therefore
      structurally forced, and the push step in `apply.yml` becomes real Python (T13).
- [x] **Tag pre-existence** — SCM rejects free-form tags (`INVALID_REFERENCE`); tags
      must exist as `scm_tag` objects. The module now creates them and orders
      tags → objects → rules. `gitops:managed` (with the colon) is a valid tag name.
- [x] **Auth end-to-end** — works with `SCM_CLIENT_ID` / `SCM_CLIENT_SECRET` /
      `SCM_SCOPE`. Scope must be `tsg_id:<TSG_ID>`; client_id is the full
      `name@<tsg>.iam.panserviceaccount.com` form.
- [x] **Ordering** — `depends_on` is sufficient; tag/address/service created before the rule.
- [x] **Concurrency** — apply MUST use `-parallelism=1`; the provider cannot handle
      concurrent token acquisition and fails with a misleading `unauthorized_client`.

⚠️ **Folder-scoped push:** a push commits *everything* staged in the folder, not just
our change. Applies must be serialized per folder, and the push step must fail closed
if unexpected staged changes are present (that delta is Level-1 drift). See
`docs/SPIKE-scm.md` → RESULTS.

**Folder ownership:** the folder must pre-exist — Day-1 provisioning owns `scm_folder`.
This module must never own it, or a Day-2 `destroy` could take the folder and all the
brownfield rules in it.

## Inputs

| Variable | Source |
|---|---|
| `address_objects` / `service_objects` / `security_rules` | `rules.auto.tfvars.json` (auto-loaded in the per-folder root) |

## Usage

See `terraform/prod-edge/` for the per-folder root that pins the backend,
configures the provider (short-lived token — design T1), and calls this module.
