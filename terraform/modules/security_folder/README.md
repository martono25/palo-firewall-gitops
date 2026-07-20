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

## ⚠️ Still open — Part B (needs the tenant + folder)

- [ ] **Commit/push model** — does `apply` push config to devices, or is a separate
      commit/push required? This is the candidate/commit boundary (docs/DESIGN.md →
      Change Rollback) and wires the atomic step in `.github/workflows/apply.yml`.
- [ ] **Tag pre-existence** — the provider takes `tag` as free-form `list(string)`,
      but SCM may reject tags that don't exist as `scm_tag` objects. If it does, add
      an `scm_tag` `for_each` over distinct tags + a dependency.
- [ ] **Auth end-to-end** — provider exposes `client_id`, `client_secret`, `scope`,
      `auth_url`, `host`, `auth_file` (feeds T1 short-lived token design).
- [ ] **Ordering** — confirm `depends_on` (objects before rules) is sufficient in practice.

## Inputs

| Variable | Source |
|---|---|
| `address_objects` / `service_objects` / `security_rules` | `rules.auto.tfvars.json` (auto-loaded in the per-folder root) |

## Usage

See `terraform/prod-edge/` for the per-folder root that pins the backend,
configures the provider (short-lived token — design T1), and calls this module.
