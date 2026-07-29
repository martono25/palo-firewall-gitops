# Changelog

All notable changes to `fwgitops` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-07-29

First production release. **Day-2 security-rule provisioning is complete and
proven end-to-end on live hardware** (VM-Series, PAN-OS 11.2.12): a rule intent
flows `intent → compile → classify → risk-gate → terraform apply → enrich → push`
and lands in the firewall's running config, verified on-device via SSH.

### Rule model — full standard-policy expressiveness
A compiled rule now carries the complete set of common security-rule fields:
- **Match:** zones, source/destination addresses, **service**, **App-ID**
  (`application`), **User-ID** (`source_user`), **URL category** (`category`),
  and **negation** (`negate_source` / `negate_destination`).
- **Action:** `allow` / `deny` / `drop` / `reset-client` / `reset-server` /
  `reset-both`.
- **Inspection & logging:** security **profile group** (`profile`), external
  **log-forwarding** (`log_forwarding`), session **log_start** / **log_end**.
- **Placement:** rulebase + ordering (`top` / `bottom` / `before:<rule>` /
  `after:<rule>`).
- **Metadata:** `description`, provenance tags.

### The `enrich` step (provider-gap workaround)
The `paloaltonetworks/scm` Terraform provider silently drops `application`,
`profile_setting`, `log_setting`, ordering, and other rule fields (v1.0.11 and
v1.0.12-beta.3; see `docs/scm-provider-securityrule-bug.md`). `fwgitops enrich`
sets them via the SCM REST API after `terraform apply` and before `push`, so the
push commits skeleton + enrichment as one atomic change. Terraform owns the rule
skeleton + state/drift/rollback; enrich owns the fields the provider can't write.

### Safety & governance
- **Fail-closed everywhere:** invalid intent, unresolvable object, or unclassifiable
  change never produces a partial result.
- **Risk classifier** with a fail-closed tier gate (LOW auto / HIGH / CRITICAL);
  checks include broad match, any-any, internet exposure, novel zone-pair,
  shadowing, `allow_without_inspection`, and `negated_match`.
- **Name catalogs** validate App-ID / profile / log-forwarding names at PR time.
- **NIST-mapped evidence bundle** per change (records the full effective rule).
- **CI:** `pr-validate` runs tests + compile + classify + enrich preview + plan;
  `apply` auto-triggers on merge (fail-closed at LOW).

### Deferred to a later release
Day-1 provisioning (interfaces / IP / zones / virtual router), `schedule`, HIP
(`source_hip` / `destination_hip`), `policy_type: Internet`, and
`tenant_restrictions`.
