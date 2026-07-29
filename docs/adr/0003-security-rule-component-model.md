# ADR-0003 — Security-rule component model (App-ID, profiles, forwarding, ordering)

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** Martono, Claude

## Context

Day-2 security-rule provisioning was proven end-to-end on live hardware, but the
compiled `SecurityRule` was a walking skeleton: it carried only zones, addresses,
service, action, `log_end`, and tags, with `application` hardcoded to `["any"]`.
A review of the authoritative `scm_security_rule` provider schema (41 fields)
against what we compile surfaced four production-critical omissions for a
next-gen firewall:

1. **`profile_setting` (security profile group)** — an `allow` rule with no
   profile permits traffic with **zero threat inspection** (no IPS / AV /
   anti-spyware / URL / WildFire). The single most important gap.
2. **`application` (App-ID)** — port-only policy is legacy L4.
3. **`position` / `relative_position` / `target_rule`** — PAN-OS is first-match;
   ordering is a correctness property, not cosmetics.
4. **`log_setting`** — without a forwarding profile, logs never leave the box.

The question was what the intent surface should *require* vs *default*, and
whether the platform should block un-inspected allows.

## Decision

Add the four components to the intent → compiler → module path as **optional,
explicit-over-magic** fields. Defaults keep every existing intent valid and
produce a plain L4 allow.

| Field (intent)   | Required | Default when omitted | Maps to (`scm_security_rule`)              |
|------------------|----------|----------------------|--------------------------------------------|
| `name` (metadata.id) | **Yes** | — fail closed    | `name`                                     |
| `application`    | No       | `["any"]`            | `application`                              |
| `profile`        | No       | **none** (uninspected) | `profile_setting = { group = [profile] }` |
| `log_forwarding` | No       | **none** (local only)  | `log_setting`                           |
| `position`       | No       | `bottom`             | `relative_position` (+ `target_rule`)      |

- **Ordering** uses the provider's native `relative_position` (`top` \| `bottom` \|
  `before` \| `after`) + `target_rule` — not a `for_each` ordering hack. The
  intent expresses it as `top` \| `bottom` \| `before:<rule>` \| `after:<rule>`.
- **Rulebase** is fixed at `position = "pre"` (managed policy lives above
  device-local rules); not requester-controlled for now.

### Un-inspected allows are surfaced, never silently shipped

`profile` defaulting to *none* is a deliberate choice (requesters opt into
inspection), but it is **not silent**: the classifier gains a stateless check
**`allow_without_inspection`** (tier **LOW**) that fires on any `allow` with no
profile group. It does not gate the change — consistent with the opt-in default
— but it lands in the evidence bundle, so the audit trail records that a human
accepted an uninspected allow. Bump to HIGH later if policy demands review.

## Live validation (2026-07-27/28) — CRITICAL provider gap found

Validated end-to-end on a live VM-Series (`prod-edge`). The intent → compile →
tfvars chain is **correct** (the module receives the right values). But **the
`paloaltonetworks/scm` Terraform provider does not write these security-rule
fields** — confirmed on stable **v1.0.11** AND pre-release **v1.0.12-beta.3**:

| Field | tfvars (correct) | committed on device |
|---|---|---|
| `application` | `[ssl, web-browsing]` | `["any"]` |
| `profile_setting.group` | `[best-practice]` | *(none)* |
| `log_setting` | `log-best` | `Cortex Data Lake` (SCM default) |
| ordering | `top` | bottom |

The provider treats these as **computed** — a fresh create drops them, and a plan
against them goes **no-op** (it won't even propose to write them). This is a
**provider gap, not an API limitation**: the SCM REST API
(`POST /config/security/v1/security-rules`, pan.dev) accepts every one of these in
the request body — `application` (required `string[]`), `profile_setting: {group:
string[]}` (doc example is literally `{"group":["best-practice"]}`), `log_setting`
(string). Rulebase `position` is a **query param** (`pre`|`post`); intra-rulebase
**ordering is a separate `Move` endpoint**, not a create attribute — so the
compiler's `position` must drive a Move, not a create field.

**Fix (BUILT + live-proven 2026-07-28): `fwgitops enrich <folder>`** — a post-apply,
pre-push step that PUTs each managed rule via the SCM API to set `application`/
`profile_setting`/`log_setting`, and issues a Move for ordering — mirroring how
`fwgitops push` does the commit the provider can't. It lands in the same candidate,
so the admin-scoped push commits skeleton + enrichment as one atomic change; and
because the provider treats these fields as computed (no-diff), it never reverts the
API-set values on later applies. Pipeline: `terraform apply → fwgitops enrich →
fwgitops push`. Live round-trip confirmed the `ScmRuleClient` PUT/Move against the
tenant (fields read back identical). `src/fwgitops/enrich.py` + `ScmRuleClient` +
`enrich` CLI + `apply.yml`; opt-in fields (`profile`/`log_forwarding`) are written
only when declared (non-destructive: an omitted field preserves the rule's current
value); `application` always reflects the declared desired state. Ordering:
`top`/`bottom` (absolute) and `before`/`after` (relative to a `target_rule`,
resolved name→UUID) via the SCM Move endpoint, applied in a second pass so targets
exist.

**Integrated pipeline PROVEN live 2026-07-28.** A full `apply → enrich → push` of a
test intent committed `application=[ssl,web-browsing]`,
`profile_setting={group:[best-practice]}`, `log_setting=log-best`, and
`position=after:REQ-…` (the rule landed immediately after its target) — all verified
on the tenant. Legacy rules that declared none of these kept their existing
`log_setting` (non-destructive confirmed); clean teardown left the folder unchanged.

**Module is skeleton-only.** `security_folder` wires only what the provider reliably
writes (zones/addresses/service/action/log_end/tag + the API-required `application`
default `["any"]`); it does NOT set `profile_setting`/`log_setting`/ordering — enrich
owns them — which removed the `log_setting` clobber churn. Object references are
fine-grained (`scm_address.this[k].name`, …) so a `terraform destroy -target` of one
object can no longer cascade into every rule (the footgun that once wiped a folder).
Steady-state plan is clean.

The evidence bundle's `compiled.rule` records the enrichment fields (application /
profile_group / log_setting / ordering) from the compiled desired-state, so the
bundle is the full audit record, not just the skeleton.

## Consequences

- Backward-compatible: existing intents compile unchanged (all new fields
  default). `tests/` cover default + explicit + fail-closed for each field.
- `terraform validate` passes; `position = "pre"` is schema- and API-vocabulary-
  verified but **not yet live-applied** (pilot VM is torn down) — one-line fix if
  the API ever rejects the value.
- **Name validation (built).** A generic `NameCatalog` (curated allowlist,
  fail-closed) validates `application`, `profile`, and `log_forwarding` against
  the firewall's known references, so a typo is caught at PR time instead of at
  the device commit. Catalogs live at `catalog/{applications,profiles,log-forwarding}.yaml`;
  **absent = that field is accepted free-form** (no false confidence). `application`
  ships populated (real App-IDs; universe = PANW Applipedia; `any` always valid);
  `profile`/`log-forwarding` ship as `.example.yaml` templates the operator fills
  with real SCM object names (listing non-existent ones would re-introduce the
  commit-time failure the catalog exists to prevent).
- Deferred (not in this ADR): richer `action` (drop/reset-*), User-ID
  (`source_user`), URL `category`, negation, `policy_type`, HIP, `description`.
