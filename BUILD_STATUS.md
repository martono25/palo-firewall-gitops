# BUILD STATUS — palo-firewall-gitops

_As of 2026-08-09, **v1.39.2**, 735 passing tests. Design + decision record:
[`docs/DESIGN.md`](docs/DESIGN.md), [`docs/adr/`](docs/adr/)._

> **This file was three weeks stale until 2026-08-09.** It described a
> 2026-07-19 snapshot — 157 tests, and the risk classifier, catalog and drift
> detection listed as *"not started"* — all three of which had shipped. A
> handoff document that a new engineer reads first, describing a system that no
> longer exists, is the same claimed-versus-actual defect this project removed
> from `expires`, from `apply.yml`'s approval claim, and from the evidence
> bundle's CM-5. It is now written to be checkable: every number below comes
> from the repo, not from memory.

## TL;DR

The Day-1 chain and the Day-2 change pipeline both run **end to end on real
hardware**. The first successful production apply was 2026-08-09 (run
`31304463821`): Terraform plan clean, SCM push committed, firewall on the newest
config version, ten evidence bundles committed to Git.

What is **not** production-ready is the surrounding process, not the engine —
see *Not built* below.

## Verified on hardware

| Capability | Evidence |
|---|---|
| Day-1 chain (Interface → Zone → Route) | applied to the pilot firewall; ADR-0002 |
| Day-2 rule pipeline | intent → compile → classify → gate → plan → apply → enrich → push |
| Cross-root apply ordering | driven by the kind registry, not a glob |
| Device scope (`device=<serial>`) | every resource accepts it after re-onboarding |
| SCM push boundary | admin-scoped; folder job 182, device job 180 |
| Drift, six checks, two engines | tag-based (rules) + state-based (zones/routes/interfaces) |
| Evidence bundles, all four kinds | 10 records committed, schema `fw-evidence/v2` |
| Removal contract | measured per kind; ADR-0008 |
| `fwgitops where` | address → intent, by CIDR containment |

## Built + tested

| Area | Module | Notes |
|---|---|---|
| Intent + validation | `intent.py` | fail-closed; unknown keys in `spec` REJECTED |
| Kind registry | `kinds.py` | one entry per kind drives compile/tfvars/classify/drift/evidence |
| Compiler | `compiler.py` | byte-stable tfvars; `Scope` owns folder-vs-device |
| Risk classifier | `classify.py` | per-kind, stateful checks, fail-closed tier gate |
| Removals | `removal.py` | tiered per kind; `Removes:` trailer authorises |
| Evidence | `evidence.py` | NIST-mapped; controls EVIDENCED, not assumed |
| Drift | `drift.py` | two engines, per scope |
| Device sync | `devicesync.py` | SCM version vs firewall running version |
| Catalog check | `catalogcheck.py` | catalog vs SCM's real hierarchy |
| Scaffolding | `scaffold.py` | Terraform roots generated from the module |
| SCM API / push / enrich | `scmapi.py`, `push.py`, `enrich.py` | retry reads, never writes |

18 CLI commands, 4 intent kinds, 8 ADRs, 26 modules.

## NOT built — what stands between this and production

Ordered by what blocks a launch, not by size. Tracked in [`TODOS.md`](TODOS.md).

| # | Gap | Status |
|---|---|---|
| 1 | **No approval path.** The tier gate BLOCKS but cannot ROUTE — a HIGH change can only be cleared by a `workflow_dispatch` override, recorded as neither approval nor override. | P1, unblocked 2026-08-09 (repo made public, so environment protection is now available) |
| 2 | **No requester intake.** `.github/ISSUE_TEMPLATE/` does not exist, so an app team must hand-write intent YAML — which contradicts the app-language premise. | next |
| 3 | **ICMP is unrequestable.** `Service` is `protocol`+`port`, tcp/udp only. Ping is the first thing anyone asks for. | needs a live probe: the provider permits a serviceless rule, but PAN-OS enforcement for one is unverified |
| 4 | **Rule ordering unwired.** Everything lands `pre:bottom`; `relative_position` needs a UUID the intent model cannot express. Shadowing is DETECTED (LOW) but not fixable. | deferred |
| 5 | **NatRequest** | deferred by decision, 2026-08-09 |
| 6 | **Second firewall / non-prod environment** | deferred by decision, 2026-08-09 |
| 7 | `push` no-op detection never fires — a no-change apply still creates SCM commit jobs | P2 |
| 8 | Removing a tag and destroying the tag OBJECT is unordered | P2 |

**Everything is validated at N=1**: one environment, one firewall, three folders.
The bugs found in this codebase have been overwhelmingly in the multi-scope paths
— device-vs-folder addressing, cross-root ordering, per-scope grouping,
aggregating routes — so (6) is a deferral with a known cost, not a free one.

## Not audited

Stated so nobody reads the table above as broader than it is: the provisioning
path, secrets handling, and the AWS state backend's own resilience have **not**
been reviewed for production. Neither has anything about multi-tenant or
multi-region operation.
