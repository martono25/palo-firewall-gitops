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

pytest -q                            # 75 tests
```

Fail-closed and all-or-nothing: if any intent is invalid, the compiler prints an
actionable report and writes nothing (exit 2). What's built so far: the tag/identity
convention, the intent schema + validator, the compiler, and this CLI. Terraform
module, pipeline, and provisioning are next (see `docs/DESIGN.md`).

## Roadmap

- **Phase 1 — Walking skeleton:** provision ONE pilot firewall end-to-end + one Day-2 rule
  flow, human-approved, on the lowest-blast-radius greenfield device group.
- **Phase 2 — Risk-tiering + evidence:** add the risk classifier (or borrow AlgoSec/Tufin/
  FireMon), enable auto-apply for low-risk classes, generate evidence bundles, add drift
  detect-and-alert.
- **Phase 3 — Scale + self-service:** SCM + Panorama backends, multi-device-group, self-service
  intake, break-glass with automated evidence.

## Before writing any code (the assignment)

1. Confirm whether **AlgoSec / Tufin / FireMon** already exists in the estate — decides
   build-vs-borrow for the risk classifier.
2. Name the **lowest-blast-radius pilot firewall + device group** for the walking skeleton.
3. Decide its **form factor** (VM-Series in which cloud / CN-Series / hardware) — sets the
   bootstrap/ZTP mechanics.

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
highest-leverage one: do we already own a commercial firewall-analysis tool?
