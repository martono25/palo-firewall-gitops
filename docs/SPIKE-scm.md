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
