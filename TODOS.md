# TODOS

Deferred work from the v2.0 engineering review (2026-07-31, branch `main`,
commit `34c9bef`). Active v2.0 work is safety fixes + the provider probe; every
item below was scoped during that review and then **deliberately deferred** when
a cross-model challenge invalidated two of its load-bearing assumptions.

**Read this first — the two findings that changed the plan:**

1. **`scm_zone` has no `tag` attribute.** Verified against `paloaltonetworks/scm`
   v1.0.11 provider schema. `scm_security_rule` has one; `scm_zone` does not.
   `src/fwgitops/drift.py` detects drift *entirely* from `gitops:` tags
   (`is_managed`, `parse_managed_meta`), so zones can never participate in the
   existing drift model. A hand-added zone on the device is permanently
   invisible. Any new intent kind must be checked for tag support **before**
   drift coverage is promised for it.
2. **ADR-0002 orders `InterfaceRequest` → `ZoneRequest`.** Zones bind interfaces.
   Without `InterfaceRequest`, the only zone that survives a device commit is one
   with an *empty* interface list — a zone that carries no traffic. "Closing the
   ZoneRequest loop" without interfaces does not produce a working zone.

## Completed

### A1 / A2 / A3 — ZoneRequest end to end — DONE v1.2.0

`scm_zone` resource + `zones` variable + module wiring; rules order after the
zones they reference (conditional reference, baseline zones pass through as
strings); full security posture (protection profile, User-ID/device-ID, log
forwarding, DoS, ACLs) with catalog validation and risk classification.

Also fixed: `_load_zone_request` built its collector without catalogs, so zone
reference names were never validated at all.

Root and module object types are byte-identical, enforced per-attribute by the
ADR-0004 contract check, so HOLE 3 cannot recur on zones.

### Zone drift detection — DONE v1.3.0

`fwgitops snapshot-zones` reads a folder's live zones (read-only) and
`fwgitops drift --zones-snapshot` compares them, wired into
`.github/workflows/drift-detect.yml`. This is the only check that can see a zone
added by hand: `terraform plan` cannot see additions, and zones carry no tags.

### InterfaceRequest (kind #3) — DONE v1.8.0

Built per ADR-0005: folder scope, `$eth-*` names, CONFIGURES an existing
interface. One registry entry drives compile, tfvars, classify and snapshot.
All four prerequisites were met first.

Drift is registry-driven too as of v1.9.0, so interfaces are covered.

### Registry-driven state drift — DONE v1.9.0

`declared_zone_state` became `declared_state(handler, objs)`, driven off the
kind's registered `tfvars` emitter. A kind declaring `drift_engine="state"` is
covered the moment it registers — previously `InterfaceRequest` declared it
while nothing wired it, so the registry made a claim the code did not keep.

Snapshots now stamp their `kind`, and drift refuses one without it rather than
guessing (mis-attributing a snapshot would compare it against the wrong declared
set entirely). `fwgitops kinds --state-drift` lets CI enumerate them, so
drift-detect.yml needs no edit when a kind is added.

### Credential redaction for published CI files — DONE v1.7.0

`.github/scripts/redact.py` strips secret VALUES from `plan-*.txt` and
`enrich-*.txt` before the artifact upload and the PR comment. GitHub masks the
live log stream but not artifact contents or `gh pr comment` bodies, and those
files capture terraform's stderr while `SCM_CLIENT_SECRET` is in the job env.

Runs with `if: always()` — a failing plan is exactly when an error carrying a
credential is most likely. One test asserts every secret the workflow injects is
in `SECRET_VARS`, so adding a secret to the job env without redacting it fails
the suite.

### Zone probe production guard — DONE v1.7.0

`spike/zone-probe` now refuses `prod-edge`, `ngfw-shared` and `All` via a
`validation` block, matching `interface-probe`. Prose in a README is not a guard.

### Schema-level contract check (HOLE 3) — DONE PR #32

`declared_object_attributes` parses a variable's `object({...})` type and
`check_object_attributes` asserts every attribute the compiler emits for that key
is declared. Wired into `run_compile`, verified by mutation: stripping the six
ADR-0003 attributes back out of `terraform/prod-edge/variables.tf` makes the
compile exit 2 and name all six. A repo-tree test compiles the real intents and
asserts the same, so root and module types cannot drift apart unnoticed.

Only the TOP level of an object type is inspected — a nested
`optional(object({...}))` whose inner attributes drift is not covered.

### Reject a ZoneRequest naming a baseline zone — DONE v1.1.0

`check_zone_collisions`. The consistency check *unions* baseline and declared
zones, so a ZoneRequest named `internet` looked maximally valid while Terraform
would have created over a live zone — which, now that zones carry a protection
profile and ACLs, would clobber them.

### T5 — Probe scm_zone field fidelity — DONE 2026-07-31, RESULT: PROVIDER IS FAITHFUL

**Result:** `paloaltonetworks/scm` v1.0.11 **writes `scm_zone` fields faithfully.**
The computed-attribute drop that breaks `scm_security_rule` does NOT apply to
zones. **Zone support needs no enrich subsystem** — A3 is a dataclass, a loader
branch and a tfvars mapping.

Probed live in folder `GitOps` (inert zone, empty interface list, local state),
created → read back over SCM REST → destroyed → absence verified:

| Field | Wanted | SCM stored |
|---|---|---|
| `enable_user_identification` | `true` | `true` |
| `enable_device_identification` | `true` | `true` |
| `network.log_setting` | `Cortex Data Lake` | `Cortex Data Lake` |
| `network.zone_protection_profile` | `best-practice` | `best-practice` |

**Second result — SCM reference-validates zone fields, fail-closed.** A
deliberately bogus `network.log_setting` was REJECTED at create:
`API_I00013 ... 'fwgitops-does-not-exist-xyz' is not a valid reference ...
type:INVALID_REFERENCE`. Nothing was created. So zones get the same reference
protection as tags on rules. Catalog validation at PR time is still worth having
(earlier feedback, sanctioned-profile restriction), but SCM is a real backstop,
not an assumed one.

**Correct API endpoints (these cost time to find — 403/400 were wrong paths and
a missing `folder` param, NOT permissions):**

| Object | Path | Note |
|---|---|---|
| profile groups | `/config/security/v1/profile-groups` | `objects/v1` returns 403 |
| zone protection profiles | `/config/network/v1/zone-protection-profiles` | **requires** `folder` param, else 400 "Operation Impossible" |
| zones | `/config/network/v1/zones` | requires `folder` param |
| log forwarding | `/config/objects/v1/log-forwarding-profiles` | lists predefined built-ins too |

**Caveat worth keeping:** the log-forwarding listing includes PREDEFINED system
objects (`Cortex Data Lake`, `IoT Security Default Profile`) that are NOT
selectable log-forwarding profiles in the SCM UI, though they ARE valid API
references. Do not treat that listing as the set of sanctioned names —
`catalog/log-forwarding.yaml` listing only `log-best` is correct.

**Probe kit:** `main.tf` + `discover.py` + `readback.py`, currently in the
session scratchpad. Worth promoting into the repo (e.g. `spike/zone-probe/`) if
this pattern gets reused for `InterfaceRequest`, which faces the same
provider-fidelity question for `scm_ethernet_interface`.

## Intent kinds

### `scm_logical_router` fidelity probe — DONE, PASSED (2026-08-02)

Ran against the live tenant in `GitOps`, read back over the SCM API, destroyed.
**All seven checked paths honored, four levels deep, and a re-plan showed no
phantom diff.** So `RouteRequest` needs no `enrich` subsystem — it is a compiler
+ tfvars mapping. Gate closed; `RouteRequest` is safe to apply.

Kit and full result table: `spike/router-probe/README.md`.

Fidelity is per resource type and still must not be extrapolated — the record is
now four for four at catching what inference would have got wrong or guessed:
`scm_security_rule` drops fields; `scm_zone`, `scm_ethernet_interface` and
`scm_logical_router` do not. **Probe before building the next kind.**

### RouteRequest (kind #4) — DONE (v1.10.0)

Shipped: one intent per route, compiler aggregates into `scm_logical_router`;
membership from `catalog/routers.yaml` resolved at load time; `drift_engine=
"state"`; `default_route` + `router_becomes_locally_owned` risk checks. Closes
ADR-0002's chain (`Interface → Zone → Route → Access`).

The fidelity probe above has since run and passed — nothing outstanding.

### NatRequest — DEFERRED to v2.0

**What:** NAT rules as an intent kind.

**Why deferred (decision, 2026-08-02):** NAT is **not** in ADR-0002's ordered
Day-1 chain (`Interface → Zone → Route → Access`), so deferring it does not block
Day-1 provisioning. `RouteRequest` is the remaining chain link and comes first.

**Context worth keeping:** `scm_nat_rule` **IS taggable** — unlike `scm_zone` and
`scm_ethernet_interface`, and like `scm_security_rule`. So `NatRequest` would
register `drift_engine="tag"` and get orphan-vs-unmanaged attribution, which the
state-based engine cannot provide. Its drift story is therefore the *rules* one,
not the zones one, and worth settling deliberately rather than by analogy to the
kind built most recently.

It also needs its own provider-fidelity probe. Rules drop fields; zones and
interfaces do not. Three kinds in, the only safe assumption is that it varies.

**Effort:** L
**Priority:** P2 (v2.0)
**Depends on:** its own fidelity probe.

### `push` no-op detection never fires — repeat pushes create empty commit jobs

**What:** `push_folder` returns `status="noop"` when SCM says there is nothing to
push (`_NOTHING_TO_PUSH` matches the error text). Against the live tenant on
2026-08-03, pushing a device with **nothing staged** returned a normal job id
(128) with `result_str=OK` and details identical to the real push (126) —
"Configuration committed successfully" — rather than an error. So the noop path
is unreachable on this code path and every push looks like it did work.

**Why it matters:** CI that pushes on every merge will mint an empty
CommitAndPush job each time, and the evidence bundle records `status="success"`
for a push that changed nothing. It also means "did my push actually commit
something?" cannot be answered from the job record — both jobs are
byte-identical apart from the id.

**Direction:** compare the candidate before/after, or diff config versions, and
derive noop from that rather than from an error string. Worth checking whether
the folder path behaves the same way (it may have been noop-detected only
because a *different* error was returned).

**Effort:** M
**Priority:** P2

## Contract enforcement

### Reject a malformed Terraform root instead of best-effort parsing

**What:** `module_arguments` scans to EOF when a module block's closing brace is
missing, so `body` becomes the rest of the file and later blocks' arguments get
attributed to it. Track whether depth returned to 0 and signal malformed input.

**Why:** Impact is bounded today — `check_contract` uses `wired_variables`, not
`module_arguments` — but the docstring promises brace-matched top-level
arguments and that does not hold. It is the natural function to reach for if
someone tightens HOLE 2 from `var.<name>` references to real module arguments.

**Effort:** S
**Priority:** P3
**Depends on:** None.

### Normalise zone names in `baseline_zones`

**What:** Whitespace-padded (`" proxy "`) and duplicate entries pass through
unnormalised into the declared set.

**Why:** A padded name silently fails to match the zone it names, producing the
same false-negative rejection `baseline_zones` was added to fix.

**Effort:** S
**Priority:** P3
**Depends on:** None.

## Drift

### State-based drift cannot tell an orphan from a hand-added object

**What:** `UNEXPECTED` collapses two causes that the tag-based engine keeps
apart: "we created it and the intent was later deleted" (orphaned) and "someone
created it by hand" (unmanaged).

**Why:** Not fixable without a provenance marker, and `scm_zone` has no `tag`
attribute. The alternative is deriving ownership from Terraform state, which
would work but couples drift detection to state-file availability — the thing
drift exists to be independent of.

**Context:** Documented in `drift.py` and surfaced in the report wording, which
deliberately does not claim to know the cause. Revisit if the provider ever
gains tags on these types, or if reading TF state proves acceptable.

**Effort:** M
**Priority:** P3
**Depends on:** None.

## CI / security

## Provisioning

## Compiler / intent model

### Zone deletion path

**What:** Design what happens when a ZoneRequest is removed from Git.

**Why:** Terraform destroys the `scm_zone`, the device commit rejects it because
a rule or interface still references it, and the candidate config is left
half-applied. The project claims fail-closed; this path is neither designed nor
tested.

**Effort:** M
**Priority:** P2
**Depends on:** An `scm_zone` resource existing.

## Architecture

### A4 / A5 — Device inventory and the inventory/intent boundary

**What:** A Git-tracked device inventory YAML per firewall (name, serial, SCM
folder, available interfaces, substrate CIDRs), driving both the provisioning
Terraform inputs and the compiler. Plus the boundary rule: inventory *describes*
substrate facts and supplies validation vocabulary; intent *reconciles* PAN-OS
config.

**Why:** `ZoneSpec.interfaces` is unvalidated free text (`intent.py:247-250`), so
a nonexistent interface fails at device commit instead of at PR time. Separately,
the SCM folder is currently declared twice — `catalog/environments.yaml:7` and
the gitignored `provisioning/aws-vmseries-pilot/terraform.tfvars` — so they can
drift invisibly. The boundary rule matters because `fwgitops drift` only compares
intents against an SCM snapshot, so anything reconcilable that lives in inventory
silently loses drift detection.

**Context — the objection that deferred this, now CONFIRMED against the tenant:**
an inventory knows *substrate* facts ("the EC2 instance has eth1/1"), but the
live tenant does not reference interfaces that way at all. Zones reference
inherited SCM *variables* (`$eth-local`), defined in the parent folder
`ngfw-shared`. So validating interface names against a device's physical
interfaces validates the wrong vocabulary — the real one is the inherited
`$eth-*` set, and resolving it requires understanding folder inheritance.
Rework the premise before building this. Secrets (`vmseries_authcode`,
`scm_registration_pin_*`) must stay out of Git regardless.

**Effort:** L
**Priority:** P2
**Depends on:** InterfaceRequest scope decision.

### Q1 — Complete the kind registry (KindHandler)

**What:** Replace the isinstance dispatch with a registered handler per intent
kind covering load, compile, tfvars emission, classify, evidence and drift.

**Why:** ADR-0001 promises a registry, but only the intent loader is genuinely
registered (`intent.py:269`). `compile_any` branches on type
(`compiler.py:111-115`), `cli.py` filters with isinstance (104-105), and
classify, evidence and drift are hard-typed to security rules
(`evidence.py:117` takes `AccessRequest`; `drift.py` uses `ActualRule`).
ADR-0001's table claiming those stages are kind-agnostic is factually wrong.
Adding a kind means touching ~8 places and remembering all of them — which is
exactly how ZoneRequest ended up wired into three stages and silently absent
from four.

**Context — why this was deferred despite being the tidiest fix:** the drift
stage cannot be implemented for zones at all (see the tag finding at the top of
this file). A protocol with optional members for stages a kind cannot support is
an interface with holes, barely better than the isinstance chain. Also, two kinds
is thin evidence for designing an abstraction, and defining it while both members
are still changing shape is how ADR-0001's table came to be wrong in the first
place. Revisit once a third kind exists and the drift story is settled.

**Note:** the `zone_tfvars` folder-scoping issue (`compiler.py:319-324` keys
globally by zone name but is only ever called per-folder) was going to be folded
into this refactor. It is latent, not an active bug, and travels with this item.

**Effort:** L
**Priority:** P3
**Depends on:** A third intent kind; a drift story for tagless objects.

### InterfaceRequest — intent kind #3

**What:** The ADR-0002 prerequisite: declare PAN-OS interface configuration
(layer3, addressing) through the normal intent pipeline.

**Why:** ADR-0002's ordered chain is `InterfaceRequest → ZoneRequest →
RouteRequest → AccessRequest`. Zones bind interfaces, so without this kind the
only zone that survives a device commit is one with an empty interface list,
which carries no traffic. Any "Day-1 provisioning as GitOps" release needs this
before zones are useful.

**Context:** This is a scope decision, not a review finding — surfaced here so
the dependency is not rediscovered later. The README currently promises Day-1
provisioning as the v2.0 target, which this is the real first step of.

**Effort:** XL
**Priority:** P2
**Depends on:** Probe scm_zone field fidelity (same provider-fidelity question
applies to `scm_ethernet_interface`).

## Performance

### P1 — Instrument apply and enrich duration

**What:** Time the apply and enrich phases, record duration in the evidence
bundle alongside the NIST mapping, surface it in the CI job summary, warn (never
fail) above a documented expectation.

**Why:** `-parallelism=1` is mandatory because the provider cannot handle
concurrent token acquisition (`apply.yml:117`), so every resource is a serialized
round trip. `enrich` is additionally N+1 by design (`enrich.py:107` and `:120`).
Change duration is also the kind of thing a change-management auditor asks for,
and it is captured nowhere today.

**Context — the counter-argument:** a realistic folder has a handful of zones, so
the "resource count is about to multiply" premise is currently hypothetical.
Revisit when resource count actually grows, or when the probe shows zones need an
enrich pass (which would roughly double apply time). Do NOT make duration a hard
CI gate — cloud latency noise would make it flaky and train people to ignore it.

**Effort:** S
**Priority:** P3
**Depends on:** Evidence that resource count is actually growing.

### Classifier scaling

**What:** `classify()` runs stateful checks by looping `for other in
policy.rule_facts` (`classify.py:360`) per change, so classification is O(N x M)
where both grow with the intent count — effectively O(N^2).

**Why:** Invisible at 6 intents. Worth knowing before the intent count reaches
the hundreds.

**Effort:** M
**Priority:** P4
**Depends on:** None. Measure before optimizing.
