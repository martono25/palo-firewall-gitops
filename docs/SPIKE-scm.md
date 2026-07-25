# Runbook — `scm` Terraform provider spike

The #1 de-risker for this project (single-plane bet on Strata Cloud Manager). It
splits into a cheap **schema spike** (no firewall needed) and a **live smoke test**
(needs a test folder; a device only for the onboarding sub-spike).

Goal: resolve every `# VERIFY:` marker in
[`terraform/modules/security_folder/`](../terraform/modules/security_folder/README.md)
and record the commit/push + tag-handling decisions that wire the pipeline.

---

## Prerequisites

**Access**
- [ ] An **SCM tenant** for testing — lab / non-prod, never production.
- [ ] An **SCM service account** with API access (also the T1 identity). Capture *how*
      it authenticates (client_id/secret? OAuth token endpoint?) — that answer is a
      spike output that feeds T1.
- [ ] A **greenfield SCM folder** for throwaway objects (never the crown-jewel rulebase — Premise 4).
- [ ] Network access to the SCM API endpoint from where Terraform runs.

**Tooling**
- [ ] Terraform ≥ 1.6
- [ ] `PaloAltoNetworks/scm` provider, pinned to a specific version
- [ ] `jq`

---

## Part A — schema spike (no firewall required)

Resolves the bulk of the checklist offline.

```bash
cd terraform/prod-edge
terraform init                                   # pins + downloads the scm provider
terraform providers schema -json > schema.json   # authoritative schema
jq '.provider_schemas' schema.json | less
```

Confirm each against `schema.json`:

- [ ] **Resource names** — `scm_address_object`, `scm_service`, and the rule resource
      (`scm_security_policy_rule` vs `scm_security_rule`).
- [ ] **Scope attribute** — `folder` vs `snippet` vs `device`; which is required.
- [ ] **Address type** — attr for CIDR (`ip_netmask`?) and FQDN (`fqdn`?), mutually exclusive.
- [ ] **Service protocol** — block shape (`protocol { tcp { port } }`?); does a port *range*
      string (`8000-8100`) work as-is.
- [ ] **Rule members** — `from`/`to`/`source`/`destination`/`service` attr names; `application`
      attr + correct "any" literal; logging attr (`log_end` vs `log_setting`).
- [ ] **Tags (highest impact on our code)** — attr name (`tags` vs `tag`); and whether free-form
      strings like `gitops:managed` are legal, OR tags must be pre-created `scm_tag` objects.
      - If tag *objects* are required: the module needs an `scm_tag` `for_each` over distinct
        tags + a dependency, and `fwgitops.tags` must emit names within SCM's charset (tighten
        `_SAFE_VALUE` if needed).

---

## Part B — live smoke test (needs the test folder)

```bash
# Minimal config: one address object, one service, one rule → the test folder.
terraform apply
```

Resolve what a schema dump can't:

- [ ] **Commit/push model** — does `apply` push config to devices, or is a separate commit/push
      step required? Watch for a "candidate / uncommitted" state. **This is the candidate/commit
      boundary (Topic 1)** and wires the `SCM commit/push` step in `.github/workflows/apply.yml`.
- [ ] **Auth confirmed end-to-end** (feeds T1 — short-lived token exchange).
- [ ] **Tag behavior confirmed** on a real object.
- [ ] **Ordering** — confirm `depends_on` (objects before rules) is sufficient.

---

## Sub-spike — device onboarding (needs a VM-Series; do with the pilot)

- [ ] Confirm the exact `init-cfg.txt` keys SCM uses to claim a device (device auth-key /
      tenant / endpoint) — resolves the VERIFY markers in
      `provisioning/bootstrap/init-cfg.sample.txt`.
- [ ] Confirm the min PAN-OS floor for SCM onboarding on the pilot hardware.

---

## Deliverables — "spike done"

1. [ ] `schema.json` committed as reference.
2. [ ] Every `# VERIFY:` in `terraform/modules/security_folder/` fixed-in-code or deleted.
3. [ ] Provider **version pinned** in both `versions.tf` files.
4. [ ] One-line decision recorded: **commit/push auto vs separate** → wires `apply.yml`.
5. [ ] Tag-handling decision: **free-form vs `scm_tag` objects** → possibly a small
       `fwgitops.tags` + module change.
6. [ ] Auth mechanism documented → feeds T1.

**Do Part A first** — a tenant + creds + folder, ~1–2 hours. If the provider doesn't cover an
object type we need, that reshapes the single-plane bet *before* time goes into the pipeline.

See also: `docs/DESIGN.md` (design + engineering review), `BUILD_STATUS.md` (what's built).

---

# RESULTS — spike complete (2026-07-19)

Provider: **`PaloAltoNetworks/scm` v1.0.11**. Part A (schema) and Part B (live
apply against the `GitOps` lab folder) both done. Module verified end-to-end:
`terraform validate` passes and a real apply created tag + address + service +
rule successfully.

## Findings

| # | Finding | Impact | Status |
|---|---|---|---|
| 1 | Resources are `scm_address` / `scm_service` / `scm_security_rule` (not `_object` / `_policy_rule`) | module | fixed |
| 2 | Tag attribute is `tag` (singular, `list(string)`) | module | fixed |
| 3 | `protocol` is a nested ATTRIBUTE (`protocol = { tcp = { port } }`), not a block | module | fixed |
| 4 | Provider is **1.0.11**; the `~> 0.9` guess would have failed `init` | pin | fixed |
| 5 | Scope must be **`tsg_id:<TSG_ID>`** — bare TSG is rejected (`invalid_scope`) | T1 / CI secret | documented |
| 6 | client_id is the full **`name@<tsg>.iam.panserviceaccount.com`** form | T1 | documented |
| 7 | **apply requires `-parallelism=1`** — the provider cannot handle concurrent token acquisition; fails with a misleading `unauthorized_client` | pipeline | fixed in `apply.yml` |
| 8 | **Tags must pre-exist as `scm_tag` objects** — the API validates them as references (`INVALID_REFERENCE`) despite the schema accepting `list(string)` | module | fixed (module creates `scm_tag`) |
| 9 | Provider has **no push/commit** capability (0/129 resources, 0/252 data sources) — `apply` only STAGES config | architecture | confirms the candidate/commit boundary |
| 10 | **Push target is the FOLDER** — a push commits everything staged in that folder, not just our change | architecture | see below |
| 11 | Folder itself must pre-exist (Day-1 owns `scm_folder`); the Day-2 module must never own it (destroy blast radius) | design | recorded |

## Consequence of #10 — folder-scoped push

Push is not per-change. Safeguards now in `apply.yml`:
1. Job-level `concurrency` serializes applies so our pipeline never stages two
   changes into one folder simultaneously (preserves 1:1 isolation + rollback).
2. **Fail closed before pushing:** if anything unexpected is staged in the folder
   (an out-of-band GUI edit), abort rather than commit someone else's unreviewed
   change under our audit trail. That staged delta is Level-1 drift and goes
   through the drift flow.

## Confirmed as designed

`tag` accepted `gitops:managed` (colon is legal in a tag name, once the object
exists) → **`fwgitops.tags` needed no change**. Rule defaults observed:
`position = "pre"`, `policy_type = "Security"` — relevant to Phase-2 sectioned
placement, which must map onto `position` / `relative_position`.

## Remaining work (not spike blockers)

- **T13** — implement the SCM push step (list staged → fail-closed check → push
  target=folder → poll job → evidence). This is now the last piece of the
  Phase-1 apply path.

## Update — Day-2 push confirmed end-to-end (2026-07-19)

Additional findings from live API testing of the push step:

| # | Finding | Impact | Status |
|---|---|---|---|
| 12 | **A folder with NO firewall bound cannot complete a push** — config stages fine, but the push has no target (`push-to invalid`). Full push success needs a device attached. | architecture | confirmed; belongs to the pilot |
| 13 | Push body key is **`folders`** (plural). The live API schema accepts `folders` (deep error `API_I00013`) and rejects `folder` (`API_I00035`). The scm-go SDK struct (`folder`) has DRIFTED from the deployed API — the live tenant is authoritative. | code | fixed (`PUSH_FOLDER_KEY`, injectable) |
| 14 | Auth scope must be `tsg_id:<TSG>`; a bare TSG → `invalid_scope`. Roleless/misscoped SA authenticates then fails on operations (`unauthorized_client`). Confirmed live. | T1 | documented |

**Track B (Day-2) is closed:** push path, verb, and body-key are confirmed against
the live tenant; the job model is the PAN-OS two-field form (`status_str` +
`result_str`). Full push *success* requires a bound device, which is a pilot/Day-1
concern, not a Day-2 code gap. Remaining SCM-endpoint work is Day-1 device
onboarding (`ScmProvisionClient`), which needs a VM-Series.

## Device onboarding sub-spike — RESOLVED (2026-07-23, from Palo docs)

VM-Series → SCM onboarding via bootstrap `init-cfg.txt` (no live device needed to
confirm — authoritative from Palo documentation):

| Key | Value | Purpose |
|---|---|---|
| `panorama-server` | `cloud` (literal) | points the firewall at SCM (TLS to the cloud service edge), not a Panorama IP |
| `vm-series-auto-registration-pin-id` | from SCM | requests a Thermite device cert to authenticate to the tenant |
| `vm-series-auto-registration-pin-value` | from SCM | " |
| `dgname` | the SCM **folder** (e.g. `GitOps`) | SCM prioritises this as the target folder |

**Prerequisites the operator provides before boot:** the auto-registration PIN
(generated in SCM device onboarding; time-limited) and — for BYOL — a VM-Series
auth code placed in the bootstrap package under `/license/authcodes`.

Applied to `provisioning/bootstrap/init-cfg.sample.txt`. The provisioning cloud
module uses Palo's current **`PaloAltoNetworks/swfw-modules/aws`** (renamed from
`vmseries-modules`).
