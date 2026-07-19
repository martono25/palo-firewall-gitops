# compiler — intent → rules.auto.tfvars.json (Python, the crown jewel)

Built on `pan-os-python` + SCM state reads. Turns app-language intent into PAN-OS objects and
rules and emits **data** (`rules.auto.tfvars.json`) that a static Terraform module consumes via
`for_each`. Generated content is DATA, never HCL. See `docs/DESIGN.md` for the 11-stage pipeline.

Key responsibilities: schema-validate → resolve entities (via `catalog/`) → normalize → infer
zones → dedup against current SCM policy → manage objects → synthesize rule → place to avoid
shadowing → emit JSON → (Phase 2) risk classify → evidence bundle.

Non-negotiables:
- **Deterministic:** same intent → same output; re-runs safe, diffs clean.
- **Stable for_each keys** (REQ-id / hash) so Terraform doesn't churn rules.
- **Shared current-policy state model** with the risk classifier (`policy/`) — one SCM reader.
- Built and tested as software (pytest), not templating.
