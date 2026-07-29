# Bug report — `paloaltonetworks/scm` Terraform provider silently drops `security_rule` fields

**Component:** Terraform provider `registry.terraform.io/paloaltonetworks/scm`
**Resource:** `scm_security_rule`
**Severity:** High — affected rules are silently created without their security profile, log-forwarding, App-ID, or ordering. An `allow` rule intended to be inspected is committed **uninspected**, with no error.

---

## Summary

When creating or updating an `scm_security_rule`, the provider **accepts these attributes in configuration but never writes them to SCM**. They are treated as computed/read-only in practice: the applied resource is committed with SCM defaults instead of the configured values, and no error or warning is raised.

Affected attributes observed:

| Attribute | Configured | Actually committed in SCM |
|---|---|---|
| `profile_setting.group` | `["best-practice"]` | *(empty / none)* |
| `log_setting` | `"log-best"` | `"Cortex Data Lake"` (SCM default) |
| `application` | `["ssl","web-browsing"]` | `["any"]` |
| `relative_position` (ordering) | `"top"` | `"bottom"` |

This is **not** an SCM API limitation — the same values succeed via the SCM REST API directly (evidence below). The defect is in the provider's create/update handling of these attributes.

---

## Environment

- Provider: `paloaltonetworks/scm` **v1.0.11** (latest stable) — **also reproduced on `v1.0.12-beta.3`**
- Terraform: v1.15.8 (darwin_arm64)
- SCM tenant (TSG): 1198884949
- Managed firewall: VM-Series `PA-VM`, PAN-OS **11.2.12**, folder-managed (folder `prod-edge`)
- Auth: service-account `client_credentials` (OAuth2), scope `tsg_id:1198884949`

The `scm_security_rule` schema in the installed provider **does** expose all four attributes (`application`, `profile_setting`, `log_setting`, `position`/`relative_position`), so this is a runtime write defect, not a missing schema.

---

## Reproduction

Minimal `scm_security_rule` (folder-scoped), applied via `terraform apply -parallelism=1`:

```hcl
resource "scm_security_rule" "repro" {
  name        = "REPRO-ADR3"
  folder      = "prod-edge"
  from        = ["local"]
  to          = ["internet"]
  source      = ["<addr>"]          # a valid scm_address
  destination = ["<addr>"]
  service     = ["<svc>"]           # a valid scm_service
  action      = "allow"

  application       = ["ssl", "web-browsing"]
  profile_setting   = { group = ["best-practice"] }   # best-practice is an inherited SCM profile group
  log_setting       = "log-best"                       # an existing log-forwarding profile in the folder
  position          = "pre"
  relative_position = "top"
}
```

`terraform apply` succeeds with no error. Then read the committed rule back.

### Observed — Terraform state after apply

```
application       = ["any"]
profile_setting   =                       # empty
log_setting       = "Cortex Data Lake"
relative_position = "bottom"
```

### Observed — SCM REST API read of the committed rule

`GET /config/security/v1/security-rules?folder=prod-edge&position=pre`

```json
{
  "name": "REPRO-ADR3",
  "application": ["any"],
  "profile_setting": null,
  "log_setting": "Cortex Data Lake"
}
```

All four configured values were dropped and replaced with SCM defaults.

### Additional observation — the provider produces a NO-OP plan for these fields

After the rule exists (committed with `application = ["any"]`), changing the config to `application = ["ssl","web-browsing"]` and re-planning yields **`No changes`** — the provider does not even propose to write the configured value. A forced `-replace` (fresh create) on `v1.0.12-beta.3` reproduces the same drop. This indicates the attributes are being treated as computed and config input is ignored during plan/apply.

---

## Proof this is a provider defect, not an API limitation

The SCM REST API accepts and persists all of these fields. A direct `POST` (bypassing Terraform) with the identical values, followed by a `GET`, returns them **unchanged**:

`POST /config/security/v1/security-rules?position=pre`

```json
{
  "name": "API-PROBE",
  "folder": "prod-edge",
  "from": ["any"], "to": ["any"], "source": ["any"],
  "destination": ["any"], "service": ["any"], "action": "allow",
  "application": ["ssl", "web-browsing"],
  "profile_setting": { "group": ["best-practice"] },
  "log_setting": "log-best"
}
```

`GET /config/security/v1/security-rules/{id}` →

```json
{
  "application": ["ssl", "web-browsing"],
  "profile_setting": { "group": ["best-practice"] },
  "log_setting": "log-best"
}
```

Same tenant, same folder, same service account — the only difference is provider-vs-API. The API round-trips the values correctly; the provider drops them.

(The API request-body schema documents all of these as writable: `application` (required `string[]`), `profile_setting.group` (`string[]`), `log_setting` (`string`). Rulebase is the `position` query param (`pre|post`); intra-rulebase ordering is the separate `POST .../{id}:move` endpoint.)

---

## Impact

- **Security:** an `allow` rule intended to carry a security profile group is committed with **no threat inspection** (no AV / anti-spyware / vulnerability / URL / WildFire), silently. For an IaC-managed firewall this scales a dangerous default across every rule.
- **Compliance/logging:** `log_setting` is not applied, so intended log-forwarding is silently absent.
- **Policy correctness:** `application` (App-ID) falls back to `any`, defeating App-ID-based policy; and rule ordering (`relative_position`) is ignored, which matters for first-match evaluation.
- All of the above happen **with a successful, error-free apply**, so the drift is invisible without an out-of-band read.

---

## Current workaround

We keep Terraform for the rule skeleton (zones/addresses/service/action/tags) and set the dropped fields via the SCM REST API in a post-apply step (`PUT /config/security/v1/security-rules/{id}` + `POST .../{id}:move` for ordering), before committing. This works because the provider treats the fields as computed and does not revert the API-set values on subsequent applies — but it should not be necessary.

---

## Request

1. Fix `scm_security_rule` create/update so `application`, `profile_setting`, `log_setting`, and `relative_position`/`target_rule` are written from configuration (not treated as computed / ignored).
2. If any of these must remain computed for a specific reason, please document it — silently dropping a configured security profile is a security-relevant surprise.
3. Confirm whether other resources with nested/optional attributes (e.g. profile groups on other rule types) share this pattern.

Happy to provide full apply logs, `terraform providers schema -json`, and a self-contained repro on request.
