# ADR-0001 — Multi-kind intent model

- **Status:** Proposed (direction accepted; build in a later phase)
- **Date:** 2026-07-27
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
