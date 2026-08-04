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

### Cross-kind ORDERING — the last unbuilt piece of ADR-0002

**What:** sequence a Day-1 build as ONE operation:
`InterfaceRequest → ZoneRequest → RouteRequest → AccessRequest`.

**Status 2026-08-04:** every KIND is built and proven on hardware — interfaces
addressed, default route active, rules enforcing on real packets. The ORDERING
is not built. The chain was sequenced BY HAND, one PR at a time.

**Why it matters.** Nothing today stops a fresh run applying a route before the
interface it depends on, or a rule before its zone. There is a cross-kind CHECK
(a rule may only use a zone its folder declares) but no sequencing, and a check
that rejects is not the same as a mechanism that orders. ADR-0002 calls this
"the real engineering" and it is still the accurate description.

It is also the difference between "the parts are built" and "Day-1 provisioning
is a thing you can run". Everything else in ADR-0002 is either done or
deliberately deferred (`NatRequest` → v2.0).

**Watch out for:** `ZoneRequest` has never reached hardware. On this tenant the
zones pre-exist in `ngfw-shared` and bind to the `$eth-*` variables, so they
attached themselves the moment the interfaces were addressed — no ZoneRequest was
needed. Do not read the chain's success as evidence that kind works end to end;
it is the one link with no live proof behind it.

**Direction:** the registry already declares per-kind capability, so it is the
natural home for a declared dependency (`InterfaceRequest` before `ZoneRequest`,
etc.) rather than a hard-coded list in the CLI. Emission is already
registry-driven per kind; ordering would follow the same shape.

**Effort:** L
**Priority:** P1 — it is the last item in ADR-0002 that is neither done nor
deferred.


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

**It also cannot tell you the DEVICE has the change.** Measured 2026-08-03: the
push job returned `FIN`/`OK` "Configuration committed successfully" while the
firewall's running config still showed the previous address. The new value
appeared **38 seconds later**. So a verification that reads the device
immediately after a successful push reads STALE config and reports success — the
failure mode is a false green, not an error.

Anything asserting "the change is live" must poll the device until it agrees,
not trust the job. `spike/`-style readbacks in this repo now do exactly that.

**Direction:** compare the candidate before/after, or diff config versions, and
derive noop from that rather than from an error string. Worth checking whether
the folder path behaves the same way (it may have been noop-detected only
because a *different* error was returned).

**Effort:** M
**Priority:** P2

### Pilot firewall: mgmt plane exposed, and no ENI behind the data interfaces

Two findings from reading the device directly on 2026-08-03 (SSH + `show`).

**1. Management plane is internet-facing.** Security group
`sg-0b9a1d2428028e4b9` allows **443 from `0.0.0.0/0`** — the PAN-OS web UI and
API, the interface that owns the device. Port 22 is correctly limited to
RFC1918. The firewall reported **98 failed admin logins since last successful
login**, so this is being probed, not merely exposed. Narrow 443 to known egress.
**Priority: P1.**

**2. The data interfaces have no physical backing.** `show interface hardware`
reports only `ethernet1/3` and `ethernet1/4`, both `down` with MAC
`00:00:00:00:00:00`. The EC2 instance has two ENIs — index 0 `10.100.0.51`
(mgmt) and index 1 `10.100.1.37` — and neither shows up as an up PAN-OS
dataplane interface.

So `REQ-2026-0801` is genuinely live in the running config
(`ethernet1/4 … 10.20.0.1/24`, zone `local`, `lr:default`) but sits on an
interface that **cannot pass traffic**. The GitOps chain is proven; the lab
topology is not wired. Attaching ENIs, or re-mapping the `$eth-*` folder
variables to an interface that has one, is a prerequisite for any traffic-level
test. **Priority: P2** (does not block the GitOps work, does block proving
traffic).

### ~~prod-edge apply would CLEAR profile_setting on REQ-2026-07302~~ — FIXED v1.15.0

Fixed by adopting provider 1.0.12-beta.4 and WIRING the fields, rather than by
`-target` or `ignore_changes`. `prod-edge` now plans `0 to change` on the rules,
and the device confirms `best-practice` and both log profiles survived the apply
and push.

The fix came from the provider's own documented example, which sets
`category = ["any"]` and `source_user = ["any"]` explicitly — omission is not
"leave alone" for an optional-NOT-computed attribute.

### `fwgitops enrich` may be retirable — 1.0.12-beta.4 writes what 1.0.11 drops

ADR-0003 exists because the provider ACCEPTS `application`, `profile_setting`
and `log_setting`, reports success, and never writes them — confirmed on v1.0.11
and v1.0.12-beta.3. **v1.0.12-beta.4 writes all three**, verified in `GitOps`
(`spike/provider-beta4`):

```
WRITTEN  application:     ['web-browsing']
WRITTEN  log_setting:     'Cortex Data Lake'
WRITTEN  profile_setting: {'group': ['best-practice']}
```

So the workaround `src/fwgitops/enrich.py` embodies may no longer be needed, and
the compiler could own these fields — which also fixes the P1 above at its root.

**Ordering is PROBED and also works** (`spike/beta4-ordering`). Three rules
created in one order, requesting another:

```
created   : alpha, bravo, charlie
requested : bravo (top), charlie (before alpha), alpha (bottom)
ACTUAL    : bravo, charlie, alpha        <- fully honoured
```

`top`, `bottom`, `before` + `target_rule` all land correctly, with no move
failure and no warning.

**Correction.** The first run of this probe reported `before` failing with a move
404 ("Failed to find obj-uuid for command get") inside a green apply, and I
attributed it to the provider. That was MY bug: I passed `target_rule` a rule
NAME. The registry docs are explicit — *"UUID of the rule to position this rule
relative to"* — and the error message said so plainly. With
`target_rule = scm_security_rule.<x>.id` it works.

**Implication for wiring it up:** the compiler emits `target_rule` as a rule
KEY, so the module must resolve it to `scm_security_rule.this[<key>].id`, not
pass the name through. A name silently produces the 404-inside-a-green-apply
described above, so the resolution is load-bearing.

**ADOPTED in v1.15.0 for the FIELDS.** `enrich` still owns before/after
ordering, which is a Terraform limitation rather than a provider one: an
anchored move needs `target_rule` as the anchor's UUID, i.e.
`scm_security_rule.this[<key>].id`, and that self-reference inside one `for_each`
block gives `Error: Cycle`.

**PROBED 2026-08-04 (`spike/ordering-existing`) — the answer is DO NOT WIRE IT.**
A first-time add of `relative_position = "bottom"` RE-STACKS the rulebase:

```
before: charlie, bravo, alpha
after:  alpha, charlie, bravo
```

and not even into for_each order (alphabetical would be `alpha, bravo, charlie`).
Each rule's move-to-bottom lands in whatever order Terraform processes the map,
which is not a guaranteed stable ordering — two runs need not agree. The plan
shows only `+ relative_position = "bottom"`, so policy is rewritten silently.

Supporting: a NO-CHANGE value is a no-op (`No changes`, Terraform does not act),
and changing the value DOES move the rule cleanly. So the mechanism is fine; the
blanket default is what is unsafe.

Also found: **Terraform cannot see ordering drift.** `relative_position` is a
create/update instruction, not a stored property, so a rule moved out-of-band
produces `No changes` on the next plan. Reordering by hand is neither detected
nor corrected.

**To wire this later**, the compiler must emit `relative_position` ONLY when the
intent explicitly asked for a position. It cannot today: `position` defaults to
`bottom`, so "unspecified" and "deliberately bottom" are the same value. That
distinction has to exist in the intent model first.

**Effort:** M — intent-model change, not a Terraform change.
**Priority:** P3 — ordering works via `enrich`; this is only about moving it. (v2.0)
**Depends on:** its own fidelity probe.

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

### An AccessRequest cannot express ICMP — ping is unrequestable

**Found 2026-08-04** when Martono asked why the traffic harness was reaching for
a NAT rule instead of just testing connectivity with ping first. It cannot:

```python
_PROTOCOLS = {"tcp", "udp"}     # src/fwgitops/intent.py
```

`Service` requires `protocol` + `port`, so ICMP has no representation. A request
as ordinary as "let the monitoring host ping this segment" cannot be written as
an intent, and the requester gets `must be one of ['tcp', 'udp']` with no hint
that the shape itself is unsupported.

**Why it matters beyond convenience.** Ping is the first thing anyone reaches
for to establish whether a path works, and the cheapest smoke test for this
platform's own changes. Without it every connectivity question has to be posed
as a TCP service that may not exist on the far side — which is exactly the
wrong-shaped test that cost hours in this session.

**Direction:** PAN-OS matches ICMP by APPLICATION (`ping`) with `service: any`,
not by a port-based service. The intent model already has an `application`
field, so the change sits in the SERVICE half: allow an application-defined
service, or accept `protocol: icmp` and compile it to `service: any` +
`application: ping`. Take care not to weaken port validation for tcp/udp — an
`icmp` protocol that silently ignores `port` would be its own trap.

**Effort:** M — intent model, compiler, and the Terraform service mapping.
**Priority:** P2


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
