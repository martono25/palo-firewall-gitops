# palo-firewall-gitops

GitOps-driven firewall automation for Palo Alto (Strata Cloud Manager / Panorama / PAN-OS),
covering **Day-1 provisioning + onboarding** through **Day-2 rule changes**, automated as far
as is safe.

> **Status: v3.0.0.** The Day-2 loop (`intent → tags ensure → compile → classify
> (the tier picks the approver) → terraform apply → enrich → push → tags sweep`)
> and the **Day-1 chain**
> (`InterfaceRequest → ZoneRequest → RouteRequest`) are both implemented, tested, and
> **proven end-to-end on live VM-Series hardware**. Four intent kinds, evidence bundles
> for every one, and drift detection across FOUR engines.
>
> **Drift is now REMEDIATED, not just reported.** Config no request authorised is
> deleted, and config Git declares is restored — unattended, nightly, with a record
> at both ends. Every class was proven against the live tenant before release:
> unmanaged object, unmanaged rule, forged copy, edited rule, deleted rule,
> reordered rulebase, disabled rule. **An emergency change made by hand now has a
> deadline** (`docs/operator-runbook.md`). See
> [`CHANGELOG.md`](CHANGELOG.md) and [`docs/adr/`](docs/adr/). Full design:
> [`docs/DESIGN.md`](docs/DESIGN.md).

## Guides

**Starting from nothing?** The four guides hand off to each other in this order,
and each one ends by pointing at the next:

```
provisioning.md  ->  building-a-folder.md  ->  requesting-rules.md  ->  operator-runbook.md
 stand up a           configure it from        add a rule              run it day to day
 firewall             Git (interfaces,
                      zones, routes)
```

**Changing a rule that already exists** — a different address, port or range —
behaves like neither adding nor removing, and has its own walkthrough:
[`changing-a-rule.md`](docs/changing-a-rule.md).

**Taking something back off a firewall** — a rule, a route, a zone, an interface
address — is its own walkthrough, written for someone who has never used this
system: [`removing-things.md`](docs/removing-things.md).

**Replacing a firewall that already exists** starts somewhere else — the serial
is threaded through the catalog, the intents and a Terraform root, and the order
matters: [`operator-runbook.md` § Replacing a firewall](docs/operator-runbook.md#replacing-a-firewall-new-serial).

| I want to… | Guide | Who |
|---|---|---|
| **Request a firewall rule** (write intent → PR) | [`docs/requesting-rules.md`](docs/requesting-rules.md) | any engineer |
| **Change a rule that already exists** (step by step) | [`docs/changing-a-rule.md`](docs/changing-a-rule.md) | any engineer |
| **Remove a rule, route, zone or address** (step by step) | [`docs/removing-things.md`](docs/removing-things.md) | any engineer |
| **Provision a firewall** (stand up a VM-Series) | [`docs/provisioning.md`](docs/provisioning.md) | platform operator |
| **Stand up a folder** (the Day-1 chain, end to end) | [`docs/building-a-folder.md`](docs/building-a-folder.md) | platform operator |
| **Operate it day to day** (a run is held, drift fired, break-glass) | [`docs/operator-runbook.md`](docs/operator-runbook.md) | platform operator |
| **Look up a command** (all 21, with exit codes) | [`docs/cli-reference.md`](docs/cli-reference.md) | platform operator |
| **Audit it** (what the evidence proves, and what it does not) | [`docs/assessor-guide.md`](docs/assessor-guide.md) | assessor / incident responder |
| Wire up CI (OIDC, secrets, environments) | [`docs/GITHUB-SETUP.md`](docs/GITHUB-SETUP.md) | platform operator |
| What each rule field maps to on the firewall | [`docs/adr/0003-security-rule-component-model.md`](docs/adr/0003-security-rule-component-model.md) | — |
| Release notes | [`CHANGELOG.md`](CHANGELOG.md) | — |

## The stack (locked)

| Layer | Choice |
|---|---|
| Platform | Palo Alto SCM / Panorama / PAN-OS |
| Reconcile engine | Terraform (`panos` + `scm` providers) — `plan` is the PR preview + drift detector |
| Logic layer | Python (`pan-os-python`) — intent compiler, risk classifier, evidence gen |
| Change model | Risk-tiered: the tier picks the approver (LOW applies with no human; HIGH/CRITICAL hold for a named reviewer) |
| Intake | Intent abstraction (app-language intent → compiler → PAN-OS); **Issue Form → generated intent → PR**, or a hand-written PR against `intent/` |
| Risk classifier | Built in-house (Python policy-as-code) — no commercial tool owned |
| CI / governance | GitHub Actions (OIDC to Palo, environment protection for the approval gate) |
| Evidence + SSoT | Git — evidence bundles Git-resident, Git is authoritative |

## What it does

**Day-1 (provisioning):** boot an NGFW → bootstrap (ZTP via `init-cfg`) → activate
licenses/subscriptions → onboard to Strata Cloud Manager (auth-key, folder/label) → push
network + security baseline. See `provisioning/`.

**Day-2 (changes):** a requester declares intent (src/dst/service/app/justification) in a
Git PR → Python compiler generates PAN-OS objects (dedup, targeting, rule placement) → risk
classifier tiers the change → **the tier picks the approver** → Terraform plans it → low-risk
applies with no human, high-risk waits for a named reviewer → every change emits a NIST-mapped
evidence bundle.

> **The tier picks the approver, and nobody types the tier.** `classify` computes it
> from the changeset (added, modified, removed) and the apply job selects its
> environment from that: `LOW` → `firewall-apply-auto`, which has no reviewer and
> applies straight through; `HIGH`/`CRITICAL` → `firewall-apply`, which has a
> required reviewer and holds. Anything that is not exactly `LOW` routes to the
> reviewed environment, so a failed classify lands on a human.
>
> **CRITICAL is not dual-controlled.** It routes to the same reviewer as HIGH.
> GitHub environment reviewers are "any one of these people approves", so a
> separate environment would give a different approver *list*, not two approvers.
>
> Demonstrated on 2026-08-10: a LOW changeset applied with no human
> ([run 31358831466](https://github.com/martono25/palo-firewall-gitops/actions/runs/31358831466)),
> and a HIGH one held for a reviewer.

```
Intent (YAML) → Python compiler → risk classifier → Terraform plan → the tier picks the approver → apply → SCM/PAN-OS
```

## Quickstart (Phase-1 compiler)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Compile intent → rules.auto.tfvars.json (per SCM folder)
fwgitops compile intent --env-map catalog/environments.yaml --out terraform
fwgitops compile intent --check      # validate only, write nothing

pytest -q                            # 865 tests
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
- **Phase 3 — Scale + self-service — IN PROGRESS.** Self-service intake is BUILT (Issue
  Form → `fwgitops from-issue` → PR), and break-glass carries automated evidence
  (`all_admins` in the bundle). Multi-folder is PROVEN: a second folder (`GitOps`) compiles,
  applies, classifies and records evidence of its own since 2026-08-15 — and found two real
  defects on its first run, a push that cannot target a folder with no firewall and an
  evidence field that could not tell a declined push from a failed one.

  Phase 3 is COMPLETE. It previously listed a Panorama backend as the remaining item, which
  contradicted the design's own resolved decision — SCM single plane, no Panorama
  ([`DESIGN.md`](docs/DESIGN.md), open question #2). Corrected 2026-08-15.
- **Day-1 as GitOps (ADR-0002) — BUILT.** The ordered chain (`InterfaceRequest` →
  `ZoneRequest` → `RouteRequest` → `AccessRequest`) is built AND run against a live firewall:
  interfaces addressed, default route active, rules enforcing on real packets. Ordering is
  declared in the kind registry and consumed by the apply pipeline (`fwgitops apply-order`).
  All four kinds are now verified on hardware: the last one, `ZoneRequest`, reached the
  pilot on 2026-08-05 (zone `dmz` bound to `ethernet1/2`, confirmed in the device's pushed
  config with its protection and log-forwarding profiles intact). Zone DELETION is
  tested end to end, and what happens depends on what still holds the zone:
  a **rule** referencing it makes SCM refuse (409 `NON_ZERO_REFS`) and the delete
  never reaches the firewall, but an **interface** bound to it does NOT — that
  deletes in about two seconds and leaves the port addressed and unzoned, which
  PAN-OS drops traffic on. Both end states fail closed; only the first fails
  loudly. Measured 2026-08-12.
  `NatRequest` remains deferred. See [`TODOS.md`](TODOS.md).

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
| `.github/ISSUE_TEMPLATE/` | Broad-requester intake: Issue Form → `fwgitops from-issue` → intent PR |
| `.github/workflows/` | CI: provision \| compile → classify → plan → route by tier → apply |

## Standing up a folder

[`docs/building-a-folder.md`](docs/building-a-folder.md) walks the Day-1 chain —
`InterfaceRequest` → `ZoneRequest` → `RouteRequest` → `AccessRequest` — from how
`prod-edge` and the pilot firewall were actually brought up, including the
prerequisites that are not in any intent file (the Terraform root, the folder
interface variables, the catalog entry) and the two things that went wrong the
first time.

## Operating it

| Command | What it answers |
|---|---|
| `fwgitops drift` | has SCM drifted from what Git declares? |
| `fwgitops device-sync` | is the FIREWALL running what SCM holds? Drift compares Git to SCM; this compares SCM to the device, which is the gap between "pushed" and "live" |
| `fwgitops verify-catalog` | does `catalog/folders.yaml` still match SCM's real hierarchy? |
| `fwgitops tags ensure \| sweep` | create the tag objects a rule references, and remove unreferenced ones. **Terraform no longer destroys tags** — it ran a tag destroy before the rule update that released it and 409'd (ADR-0009), so the halves are separated in time |

`ensure` runs before apply and `sweep` after push; the pipeline does both, so you
only reach for them by hand when investigating.

### Flags the pipeline drives, and what breaks without them

Three flags exist for CI rather than for you. They are documented because each
has a failure mode that is silent, and all three have produced one.

| Flag | Why it exists |
|---|---|
| `classify --max-tier` | prints **one line** — the highest tier in the changeset — so the workflow can route on it. Everything else goes to a buffer; a stray second line made `$GITHUB_OUTPUT` reject `Invalid format 'HIGH'`, and only a changeset containing a removal ever produced one |
| `classify --change-message` | a removal authorises itself with a `Removes:` trailer in the commit message, because the change **is** the deletion of the file. Without this the tier step cannot see the authorisation and no removal can be applied at all |
| `push --record` → `evidence --push-record` | carries the SCM commit job into the evidence bundle. Without it a bundle proves Terraform applied the change and says nothing about whether it reached SCM — and applied-but-unpushed is a state this platform has actually been in |

```sh
fwgitops push --scope-dir prod-edge --record push-prod-edge.json
fwgitops evidence intent --push-record push-prod-edge.json --baseline /tmp/base/intent
```

The record carries `admin_count` and `all_admins`, never an identity: pushes are
scoped to `SCM_CLIENT_ID`, and the bundle is committed to a public repository.
Run `evidence` without `--push-record` or `--baseline` and it says so — a run
that cannot see removals, or cannot show delivery, should not look like one that
found nothing.

## Incident response: `fwgitops where`

A firewall log gives an IP. The question is *which request permitted this, who
asked for it, and under what ticket* — and `grep` answers it **wrong**, not
slowly: the log says `10.20.9.10`, the intent says `10.20.9.0/24`, so grep
returns nothing. Nothing is the worst available answer, because it is
indistinguishable from "no rule permits this".

```
$ fwgitops where 10.20.1.55

RULES — what permits or denies it
  AccessRequest  REQ-2026-0725  (prod-edge)
      matched : address_objects[0].value = 10.20.1.0/24
      why     : 10.20.1.0/24 contains 10.20.1.55
      ticket  : JIRA-20725  (martono@corp, 2026-07-25)
      request : Web tier ships logs/metrics to the central observability collector
      intent  : intent/prod/observability/REQ-2026-0725.yaml
      evidence: evidence/prod-edge/REQ-2026-0725.json

ROUTES — what carries it
  RouteRequest  REQ-2026-0803  (prod-edge)   <- CARRIES IT
```

Also accepts a CIDR, a zone/app/interface name, a request id, a ticket, or a
requester. `--json` for piping into an incident timeline.

Three things it is careful about:

- **What PERMITS and what CARRIES are separate questions.** A default route
  matches every address, so a flat match count would report "1 match" for
  traffic nothing permits — the opposite of the truth. When no rule mentions the
  address, that is stated, not implied by absence.
- **It searches the COMPILED state.** An intent may name an app whose addresses
  live in the catalog, so the CIDR appears nowhere in the intent file.
- **Nothing found is an ANSWER**, not an error (exit 4): it means the config came
  from somewhere else, and it points at `fwgitops drift`.

## Open questions

See the [Open Questions](docs/DESIGN.md#open-questions) section of the design doc. The
build-vs-borrow question is settled: no commercial firewall-analysis tool is owned, so the
risk classifier is built in-house.

The open ones now are scoping questions, tracked with their reasoning in
[`TODOS.md`](TODOS.md): how much of the device model belongs in Git-tracked YAML, whether
zones can ever join the tag-based drift model (`scm_zone` has no `tag` attribute), and what
comes after the Day-1 chain (`NatRequest`, a second firewall, a non-prod environment — all
deferred there).
