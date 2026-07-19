# CI/CD — the governance layer

GitHub Actions is where run history, RBAC, scheduling, and OIDC-to-cloud live
(replacing Ansible/Tower). Two workflows implement the Phase-1 flow:

```
PR opened ──▶ pr-validate.yml ──▶ compile (fail-closed) + tfvars-in-sync check + terraform plan → PR comment
merge  ─────▶ apply.yml       ──▶ [firewall-apply env = human approval] → terraform apply per folder → SCM commit/push
```

## Phase 1 (implemented, unvalidated)

- **pr-validate.yml** — validates intent fail-closed, checks the committed
  `rules.auto.tfvars.json` matches a fresh compile, and posts a `terraform plan`
  preview to the PR. No apply.
- **apply.yml** — on merge to `main`, applies the reviewed desired-state behind
  the `firewall-apply` GitHub Environment (required reviewers = human approval on
  every change, since there's no risk classifier yet). One folder at a time
  (1:1 isolation); objects-before-rules via the module's `depends_on`; the SCM
  commit/push is the atomic candidate/commit boundary (Topic-1 rollback).
- **CODEOWNERS** — folder-level review gate (the HIGH-tier standard approver).

## Phase 2 (marked slots, not built)

- **classify** job → risk tier (LOW/HIGH/CRITICAL) from `fwgitops classify`.
- **Tier-driven environments**: LOW auto, HIGH → `firewall-review`, CRITICAL →
  `firewall-security` (dual-control, maintenance-window only).
- **Window-aware scheduler** — tier-driven cadence, 1:1 isolation, batching off.
- **Drift detection** — scheduled workflow (Level-1 tag-scoped + Level-2 device).

## Setup checklist (before these run for real)

- [ ] `firewall-apply` Environment with required reviewers (Settings → Environments).
- [ ] Cloud OIDC role/SA for the TF state backend (Arch-2) + `id-token` trust.
- [ ] Short-lived SCM token exchange (T1) — replace the `Mint short-lived SCM token` stubs.
- [ ] Real team handles in `CODEOWNERS`.
- [ ] Backend filled in (`terraform/<folder>/backend.tf`).
- [ ] `scm` provider spike done (`terraform/modules/security_folder/README.md`).
