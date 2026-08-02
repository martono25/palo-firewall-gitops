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

## Contract enforcement

### Schema-level tfvars contract check (attributes, not just keys)

**What:** Extend `tfcontract.check_contract` to parse
`variable "k" { type = map(object({...})) }` and assert every ATTRIBUTE the
compiler emits for that key is declared — not just that the top-level key exists.

**Why:** This is HOLE 3, and key-name matching structurally cannot see it.
Terraform's object-to-object conversion silently discards attributes the target
type does not declare: no warning, no diagnostic, exit 0. It was live in v1.0 —
`terraform/prod-edge/variables.tf` omitted the six ADR-0003 attributes while the
module declared them and the compiler emitted them, so App-ID, profile group and
log setting never reached the module. The instance is fixed (types are now
identical) but nothing stops them drifting apart again, and the comment claiming
they were "kept in sync" was already false once.

**Context:** The masking + comment-stripping machinery in `tfcontract.py` already
does the hard part. The remaining work is extracting attribute names from an
`object({...})` type expression and comparing against the inner keys of the
emitted payload. A cheaper interim: a test asserting the root and module
`variables.tf` object types are byte-identical.

**Effort:** M
**Priority:** P1
**Depends on:** None.

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

## CI / security

### Keep terraform stderr out of published artifacts and PR comments

**What:** `pr-validate` folds stderr into `plan-$folder.txt` (`2>&1 | tee`),
which is uploaded as an artifact and pasted into a PR comment. The job env holds
`SCM_CLIENT_SECRET`.

**Why:** GitHub's secret masking applies to the log stream, not to artifact file
contents or `gh pr comment` bodies. Any provider or auth error that echoes a
credential would reach a durable artifact and a public PR comment unredacted
while the visible log looked clean. Not observed — the risk is structural.

**Context:** Options are dropping `2>&1`, tee-ing stderr to a separate
unpublished file, or scrubbing (`sed "s/${SCM_CLIENT_SECRET}/***/g"`) before the
tee. The artifact is genuinely useful for debugging a failed plan, so prefer
scrubbing over dropping.

**Effort:** S
**Priority:** P2
**Depends on:** None.

### Guard the zone probe against pointing at production

**What:** `spike/zone-probe/main.tf`'s `folder` variable has no validation, so
`-var 'folder=prod-edge'` creates a real object in the production folder against
live credentials. The only guard is prose in the README.

**Why:** A copy-paste or shell-history recall is all it takes, and the probe runs
with write-capable SCM credentials.

**Context:** A `validation` block rejecting known production folders (or an
allowlist of scratch folders) is a few lines.

**Effort:** S
**Priority:** P2
**Depends on:** None.

## Provisioning

### Decide what InterfaceRequest manages, BEFORE probing any interface resource

**What:** Settle whether `InterfaceRequest` manages the shared-folder interface
variable, a folder-local override of it, or nothing at all because bootstrap
owns interface bring-up. Only then probe that resource's provider fidelity.

**Why:** ADR-0002 specifies `InterfaceRequest` as `ethernet1/1 layer3,
DHCP/static IP` — a folder-local interface carrying addressing. Read-only
discovery on 2026-08-02 showed the tenant does not work that way: interfaces are
named variables (`$eth-local`, `$eth-internet`) with per-device `default_value`s
(`ethernet1/4`, `ethernet1/3`), defined once in the parent folder `ngfw-shared`
and inherited by both `prod-edge` and `GitOps`, with `layer3 = {}` — no
addressing stored in SCM at all.

So the design in ADR-0002 has no target as written, and an `InterfaceRequest`
would either write to a folder feeding production *and* the sandbox, or create
local overrides of inherited objects. Both are materially different, and the
first carries more blast radius than any Day-2 change so far.

**Context:** The `scm_ethernet_interface` fidelity probe was deliberately NOT
run — fidelity of a resource the design may not use is not the blocker, and
unlike zones there is no clean scratch target (ngfw-shared feeds production;
GitOps would create an override). The probe kit at `spike/zone-probe/` is ready
to point at whichever resource this decision lands on. Full write-up in ADR-0002.

**Effort:** M (decision) + S (probe, once the target is known)
**Priority:** P1
**Depends on:** None — this is the gate on the whole Day-1 chain.

## Compiler / intent model



### Reject a ZoneRequest naming a baseline zone

**What:** Fail compile when a ZoneRequest's zone name already appears in
`env_map.baseline_zones_by_folder()`.

**Why:** `check_zone_consistency` (`compiler.py:343-345`) *unions* baseline and
declared zones, so a ZoneRequest named `internet` looks maximally valid. Terraform
would attempt a create against an object that already exists on the device, and
under A3 that create carries `zone_protection_profile`, `log_setting` and
`user_acl` — clobbering a live baseline zone on the production edge folder.

**Context:** Small, self-contained, and worth doing even if all other zone work
stays deferred. Tracked as task T7 in the review's task list.

**Effort:** S
**Priority:** P2
**Depends on:** None.

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
