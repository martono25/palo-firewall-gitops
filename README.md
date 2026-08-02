# palo-firewall-gitops

GitOps-driven firewall automation for Palo Alto (Strata Cloud Manager / Panorama / PAN-OS),
covering **Day-1 provisioning + onboarding** through **Day-2 rule changes**, automated as far
as is safe.

> **Status: v1.0 — Day-2 rule provisioning shipped.** The full Day-2 loop
> (`intent → compile → classify → risk-gate → terraform apply → enrich → push`) is
> implemented, tested, and **proven end-to-end on live VM-Series hardware** (rule
> verified in the device running config). See [`CHANGELOG.md`](CHANGELOG.md) and
> [`docs/adr/`](docs/adr/). Day-1 provisioning is the v2.0 target. Full design:
> [`docs/DESIGN.md`](docs/DESIGN.md).

## Guides

| I want to… | Guide | Who |
|---|---|---|
| **Request a firewall rule** (write intent → PR) | [`docs/requesting-rules.md`](docs/requesting-rules.md) | any engineer |
| **Provision a firewall** (stand up a VM-Series) | [`docs/provisioning.md`](docs/provisioning.md) | platform operator |
| What each rule field maps to on the firewall | [`docs/adr/0003-security-rule-component-model.md`](docs/adr/0003-security-rule-component-model.md) | — |
| Release notes | [`CHANGELOG.md`](CHANGELOG.md) | — |

## The stack (locked)

| Layer | Choice |
|---|---|
| Platform | Palo Alto SCM / Panorama / PAN-OS |
| Reconcile engine | Terraform (`panos` + `scm` providers) — `plan` is the PR preview + drift detector |
| Logic layer | Python (`pan-os-python`) — intent compiler, risk classifier, evidence gen |
| Change model | Risk-tiered auto-apply (low-risk auto, high-risk human-gated) |
| Intake | Intent abstraction (app-language intent → compiler → PAN-OS); broad requesters via GitHub Issue Forms |
| Risk classifier | Built in-house (Python policy-as-code) — no commercial tool owned |
| CI / governance | GitHub Actions (OIDC to Palo, environment protection for the approval gate) |
| Evidence + SSoT | Git — evidence bundles Git-resident, Git is authoritative |

## What it does

**Day-1 (provisioning):** boot an NGFW → bootstrap (ZTP via `init-cfg`) → activate
licenses/subscriptions → onboard to Strata Cloud Manager (auth-key, folder/label) → push
network + security baseline. See `provisioning/`.

**Day-2 (changes):** a requester declares intent (src/dst/service/app/justification) in a
Git PR → Python compiler generates PAN-OS objects (dedup, targeting, rule placement) → risk
classifier tiers the change → Terraform plans it → low-risk auto-applies, high-risk waits for
human approval → every change emits a NIST-mapped evidence bundle.

```
Intent (YAML) → Python compiler → risk classifier → Terraform plan → tier gate → apply → SCM/PAN-OS
```

## Quickstart (Phase-1 compiler)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Compile intent → rules.auto.tfvars.json (per SCM folder)
fwgitops compile intent --env-map catalog/environments.yaml --out terraform
fwgitops compile intent --check      # validate only, write nothing

pytest -q                            # 557 tests
```

Fail-closed and all-or-nothing: if any intent is invalid, the compiler prints an
actionable report and writes nothing (exit 2). The same applies to the
**compiler → Terraform contract** — compiling data that no Terraform module
declares and wires is an error, not a silent no-op (ADR-0004).

Built and proven end-to-end on live hardware: the tag/identity convention, the
intent schema + validator, the compiler, the risk classifier, the Terraform
module, `enrich`, `push`, drift detection, evidence bundles, the CI pipeline,
and Day-1 bootstrap + SCM onboarding. See [`CHANGELOG.md`](CHANGELOG.md) and
[`docs/adr/`](docs/adr/) for what is built versus designed, and
[`TODOS.md`](TODOS.md) for deferred work.

## Roadmap

- **Phase 1 — Walking skeleton — DONE.** One pilot VM-Series provisioned end-to-end and one
  Day-2 rule flow proven in the device running config.
- **Phase 2 — Risk-tiering + evidence — DONE.** Risk classifier built in-house (no commercial
  tool in the estate), auto-apply at LOW, NIST-mapped evidence bundles, drift detect-and-alert.
- **Phase 3 — Scale + self-service — NEXT.** Multi-folder, Panorama backend, self-service
  intake, break-glass with automated evidence.
- **Day-1 as GitOps (ADR-0002) — DATA PLANE COMPLETE.** The bootstrap half was already built;
  the ordered data-plane chain (`InterfaceRequest` → `ZoneRequest` → `RouteRequest` →
  `AccessRequest`) is now expressible end to end, each kind probed against the live provider
  before being declared safe to apply. See [`TODOS.md`](TODOS.md).

The three questions this project opened with are answered: no commercial firewall-analysis
tool is owned (classifier built in-house), the pilot is a greenfield SCM folder, and the form
factor is VM-Series on AWS.

## Layout

| Path | Purpose |
|---|---|
| `docs/` | Design doc and decision record |
| `provisioning/` | Day-1: NGFW bring-up, bootstrap, licensing, SCM onboarding |
| `intent/` | Day-2: intent YAML request surface (app-language, not PAN-OS) |
| `catalog/` | Platform-maintained: app→subnets/zones, service→port, ip→zone map |
| `compiler/` | Python: intent → `rules.auto.tfvars.json` (data, not HCL) |
| `policy/` | Risk classifier (Python) — shares current-policy state model with compiler |
| `terraform/` | Day-2 reconcile state, split per SCM folder; static module + `for_each` |
| `evidence/` | Git-resident NIST-mapped evidence bundles (Git = SSoT) |
| `.github/ISSUE_TEMPLATE/` | Broad-requester intake: Issue Forms → Action → intent PR |
| `.github/workflows/` | CI: provision \| compile → classify → plan → gate → apply |

## Open questions

See the [Open Questions](docs/DESIGN.md#open-questions) section of the design doc. The
build-vs-borrow question is settled: no commercial firewall-analysis tool is owned, so the
risk classifier is built in-house.

The open ones now are scoping questions, tracked with their reasoning in
[`TODOS.md`](TODOS.md): how much of the device model belongs in Git-tracked YAML, whether
zones can ever join the tag-based drift model (`scm_zone` has no `tag` attribute), and what
v2.0 covers now that the Day-1 chain is closed (`NatRequest` is deferred there).
