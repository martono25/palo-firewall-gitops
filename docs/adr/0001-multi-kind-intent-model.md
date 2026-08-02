# ADR-0001 — Multi-kind intent model

- **Status:** Accepted — **partially built** (see *Implementation status* below)
- **Date:** 2026-07-27 (status revised 2026-07-31)
- **Deciders:** Martono, Claude

## Context

Today the platform has exactly one intent kind — `AccessRequest` → a security
rule. But managing a firewall is more than policy: **zones, interfaces, IP
addresses, virtual routers, NAT, and address/service groups** are all
configuration an administrator changes on a live firewall, not just at bootstrap.

This surfaced concretely through the app catalog. An app carries a `zone`
(e.g. `web-tier = local`). If an intent references an app whose zone does not
exist on the firewall, nothing catches it until the **device-side commit fails**
("zone 'X' is not a valid reference") — the same last-mile failure we hit when
the env map used `trust`/`app`. Forcing zones into "bootstrap only" would
misrepresent how firewalls are actually operated (admins add a zone today, write
rules for it tomorrow — both should flow through Git).

## Decision

Make **intent `kind` the extension point.** Introduce a *kind registry* where
each kind plugs in exactly three things, and the rest of the pipeline stays
kind-agnostic:

| Stage | Kind-specific? |
|---|---|
| validate / load schema | **yes** — each kind has its own `spec` shape |
| compile → Terraform | **yes** — `AccessRequest`→`scm_security_rule`, `ZoneRequest`→`scm_zone`, … |
| risk classify | **yes** — a zone add ≠ an any-any allow risk-wise |
| gate · apply · push · evidence · drift | **no** — generic (operate on compiled objects + `gitops:` tags) |

> **The last row was wrong, and it mattered.** It is kept above as originally
> written because it is what the design claimed; see *Implementation status* for
> what the code does. Drift in particular cannot be made generic the way this
> row assumes — see *Correction: drift is not kind-agnostic*.

## Implementation status (2026-07-31)

Built (PRs #18–#20, completed v1.2.0): the intent-loader registry
(`_KIND_LOADERS` in `intent.py`), `ZoneRequest` as kind #2 — now compiling to a
real `scm_zone` with its full security posture, catalog-validated and
risk-classified — and the cross-kind zone-consistency check. `AccessRequest`
remains the only kind proven end-to-end on live hardware; `ZoneRequest` is
proven to the SCM API by the ADR-0004 probe but has not been pushed to a device.

Note the classifier gained `classify_zone` as a SEPARATE entry point rather than
a branch inside `classify`. The rule classifier's vocabulary (address sets, port
spans, shadowing) is meaningless for a zone. One small function per kind is
honest; the registry refactor below would replace both with one dispatch.

**Not built — the registry is a registry in name only.** Only the intent loader
is genuinely registered. Everything downstream branches on Python type:
`compile_any` uses an `isinstance` chain, `cli.py` filters with `isinstance`,
and `classify` / `evidence` / `drift` are hard-typed to security rules
(`build_bundle` takes an `AccessRequest`; `drift` works on `ActualRule`).
Adding a kind means touching ~8 places and remembering all of them.

That is not hypothetical. `ZoneRequest` was wired into the three stages someone
remembered (loader, compiler, CLI) and silently omitted from the four nobody did
(Terraform, classify, evidence, drift). Because Terraform ignores an undeclared
auto-tfvars variable with exit 0, the result compiled, planned and applied green
for an entire release without ever reaching a firewall. See ADR-0004.

### Correction: drift is not kind-agnostic (and the registry says so)

`drift.py` detects drift entirely from `gitops:` tags (`is_managed`,
`parse_managed_meta`), and the module carries a whole `scm_tag` resource because
SCM validates tags as references. **`scm_zone` has no `tag` attribute** — verified
against provider v1.0.11. So zones cannot participate in the tag-based drift
model at all: a hand-added zone is invisible, and there is no `gitops:req`
provenance for a zone.

Both engines now exist (v1.3.0): tag-based for rules, state-based for objects
that cannot carry tags. `KindHandler.drift_engine` records which a kind uses, so
"does this kind have drift coverage" is answerable without guessing.

Before promising drift coverage for a future kind, check whether its SCM resource
supports tags — `scm_ethernet_interface` does not either, and only 14 of the
provider's resources do.

- `AccessRequest` is kind #1 (built + proven). `ZoneRequest` is kind #2. Grow as
  we mature: `InterfaceRequest`, `RouteRequest`, `NatRequest`, group kinds.
- The one genuinely new mechanism this requires is **cross-kind dependency
  ordering**: infrastructure kinds apply before the policy kinds that reference
  them (a `ZoneRequest: dmz` before any `AccessRequest` that uses `dmz`).

## Consequences

**Positive**
- The whole governance loop (classify → gate → evidence → drift) is reused for
  every object type — network changes get reviewed, not buried.
- **Consistency by construction:** an `app.zone` is valid iff a `ZoneRequest`
  declared it; policy referencing a zone validates against the declared set at
  PR time, not at the device commit.
- Git stays the single source of truth for the *entire* device config.
- Incremental: introduce the dispatch seam first (keep `AccessRequest` green),
  then add kinds one at a time.

**Negative / cost**
- A core refactor: generalize load/compile/classify around the registry.
- Cross-kind ordering is real engineering (dependency graph across kinds).

## Related
- ADR-0002 (Day-1 provisioning) depends on this model.
- Recommended first step: build the kind-dispatch seam, register `AccessRequest`,
  then add `ZoneRequest`.
