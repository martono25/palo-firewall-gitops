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

## v2.0 — rule provisioning

### ~~Rules are FOLDER-SCOPE ONLY~~ — RETRACTED. Device scope works.

**This was wrong for one day and is corrected here rather than deleted.**

Three spikes concluded that zones, logical routers and security rules were
folder-scope only, each on the same evidence: a device-scope create refused with
`"Device <serial> doesn't exist"` while an ethernet interface succeeded on the
same firewall in the same run.

The firewall was in a broken registration state. After it was offboarded and
re-onboarded into SCM, **every resource accepts a device-scope write** —
interface, zone, logical router, address, tag and security rule. Reproduced three
times with readback and cleanup.

**Why the control did not catch it.** Every probe used `scm_ethernet_interface`
as its positive control, and it passed every time — because it was the one
resource still working while the device was broken. "Interface works, rule does
not" therefore read as RESOURCE-SPECIFIC when it was DEVICE PARTIALLY BROKEN. A
positive control proves the path is alive; it does not prove the environment is
healthy, and it is worth least exactly when the passing case is the anomaly.

**The error message was literally true.** It said the device was not registered
for configuration. It was dismissed as misleading because the device reported
`is_connected: true` and every GET worked — read paths and config-write paths do
not share that registration. **When an error names a precondition, test the
precondition** instead of arguing from adjacent evidence that it must be wrong.

**Fixed in code:** `_load_zone_spec` and `_load_route_spec` rejected `device:` on
the strength of this. Both now accept it, and the `allow_device` mechanism was
removed rather than left as dead code carrying a wrong rationale. It can return
if a resource is ever shown to be folder-only — with evidence gathered against a
healthy device.

### ~~Rule targeting~~ — DECIDED, ADR-0007

`AccessRequest` targets `environment:` only. `folder:` and `device:` stay
rejected, now with a message that gives the reason and cites the ADR rather than
reporting a generic unknown field.

**The line:** device scope is for CONFIGURATION; the unit of POLICY is the
folder. An interface address is genuinely per-firewall — two firewalls cannot
share one — so `InterfaceRequest` must name a device. A rule that applies to one
firewall and not its neighbours is a policy OVERRIDE, and per-firewall divergence
is something an operator reasons about for as long as it exists.

Decided on merits, not inherited: SCM DOES accept device-scope security rules.
The earlier "it is a constraint" reading came from three spikes run against a
broken device registration.

**Per-firewall policy costs a folder**, and that is the intended price. It is now
a PR (`scaffold-root` + a catalog entry), the empty-folder warning catches the
half-finished state, and the cost is proportionate to asking for a firewall whose
policy diverges from its fleet.

**`folder:` for platform-authored rules is NOT added.** Plausible future need, no
current one — and this repo deleted three fields in a week that were declared,
stored and never read (`app.folder`, `metadata.expires`, `devices.hostname`). Add
it with the case that motivates it.

## Intent kinds

### ~~Cross-kind ORDERING — the last unbuilt piece of ADR-0002~~ — BUILT v1.17.0

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

**BUILT v1.17.0**, in the registry as anticipated: `KindHandler.depends_on_kinds`
+ `kind_apply_order()` (topological, deterministic, fails closed on a cycle or an
unregistered dependency), and `fwgitops apply-order` for the pipeline, which no
longer relies on glob order.

**Still open, and NOT fixed by ordering:** `ZoneRequest` has never reached
hardware. On this tenant the zones pre-exist in `ngfw-shared` and self-attach, so
the chain's success is not evidence for that kind. It needs a firewall in a fresh
folder to exercise.


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

### ~~Pilot firewall: mgmt plane exposed, and no ENI behind the data interfaces~~ — BOTH FIXED

Two findings from reading the device directly on 2026-08-03 (SSH + `show`).

**1. Management plane is internet-facing.** Security group
`sg-0b9a1d2428028e4b9` allows **443 from `0.0.0.0/0`** — the PAN-OS web UI and
API, the interface that owns the device. Port 22 is correctly limited to
RFC1918. The firewall reported **98 failed admin logins since last successful
login**, so this is being probed, not merely exposed. Narrow 443 to known egress.
**Priority: P1.** — **FIXED.** That SG no longer exists; the pilot now carries
`fwgitops-pilot-mgmt-…` (`sg-0720301d5a39a397f`) with 22 AND 443 both limited to
a single known egress `/32`. `provisioning/aws-vmseries-pilot/variables.tf` now
validates `mgmt_allowed_cidr` and REFUSES `0.0.0.0/0`, so the exposure cannot be
reintroduced by editing a tfvar — verified on the live SG 2026-08-04.

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
test. **Priority: P2** — **FIXED.** The instance now has FIVE ENIs (indices 0-4:
mgmt `10.100.0.51`, plus `10.100.1.37`, `10.100.1.110`, `10.100.2.142`,
`10.100.3.125`), the dataplane SG has VPC ingress, and the firewall has since
seen and enforced real packets. Verified on the live instance 2026-08-04.

**What this does NOT close:** the traffic ROUND TRIP was never completed — the
return leg was still failing when the test hosts were destroyed. The harness
survives in `provisioning/aws-vmseries-pilot/traffic-test.tf` behind
`enable_traffic_test = false`; the ARP-poisoning and SG-ingress traps that cost
the most time are documented there.

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

Ordering itself is a separate item — see *Rule ORDERING via `relative_position`*
below, deferred to v2.0.

### Rule ORDERING via `relative_position` — DEFERRED to v2.0

**What:** move before/after rule ordering out of `src/fwgitops/enrich.py` and
into the compiler + Terraform module, so the whole rule is one declarative write.

**Why deferred (decision, 2026-08-04):** it needs an INTENT-MODEL change, not a
Terraform change, and the intent model is not the thing v1.x is stabilising.
`enrich` orders rules correctly today, so this buys tidiness, not capability —
and buying it wrong rewrites a live rulebase (below). It sits alongside
`NatRequest` as v2.0's compiler work.

**PROBED 2026-08-04 (`spike/ordering-existing`) — DO NOT WIRE IT AS IT STANDS.**
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
**Priority:** P2 (v2.0) — not urgent, but it is now a scoped v2.0 item rather
than a someday-maybe. Ordering works via `enrich` in the meantime.
**Depends on:** an intent model that can express "unspecified" separately from
"bottom". Its provider fidelity probe is DONE (above) and passed.

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

### Removing a tag from a rule and destroying that tag OBJECT is UNORDERED

**Found 2026-08-05 while removing the expiry tag, and it is latent for any tag
change — not specific to expiry.**

When a tag value stops being used, one apply contains two actions: UPDATE the
rules to drop the reference, and DESTROY the now-unused `scm_tag`. Terraform has
no reason to order them, because after the change the rule's config no longer
REFERENCES `scm_tag.this[<tag>]` — the dependency edge that ordered creation
disappears exactly when it is needed for destruction. It destroyed first:

```
409 NON_ZERO_REFS
"Node cannot be deleted because of references from
 container -> prod-edge -> pre-rulebase -> security -> rules -> REQ-2026-0727 -> tag"
```

SCM fails closed, so nothing was corrupted — the same guard the zone deletion
test found. Here it turns into an apply failure instead of a safeguard.

**`-target` does not help:** targeting the rules pulls the tag in as a
dependency and plans the destroy anyway.

**The migration workaround was:** `terraform state rm` the tag objects, apply the
rule updates, then delete the orphaned tag objects via the API. Fine once, not a
design.

**Why there is no clean declarative fix:** the needed edge is "destroy the tag
AFTER the rules that referenced it are updated", and Terraform only derives edges
from references that still exist. A blanket `depends_on = [scm_tag.this]` on the
rules would create it — and is exactly the pattern this module removed once
already, because it made every rule depend on every object instance and a
`destroy -target` of one address cascaded into destroying ALL rules.

**Likely answer:** stop destroying tag objects in the same apply as the rules
that release them. An unused `scm_tag` is inert, and a separate sweep (the
folder-interfaces / verify-catalog shape) could remove them once nothing
references them. That trades a failed apply for a little garbage.

**This bites any tag VALUE change**, e.g. a corrected ticket number. It has not
been hit before only because no tag value has ever changed on a live rule.

**Effort:** M
**Priority:** P2

### ~~`spec:` ignores unknown keys~~ — BUILT v1.25.0

All four spec loaders now reject unknown fields, closing the class the metadata
guard started. `spec` was the sharper half: it is where firewall behaviour lives,
so a dropped key is a rule that does not do what it says and looks fine doing it
(`logging: true` compiled clean and produced a rule logging at its default — no
plan diff, no warning, no failed apply).

The allow-lists are pinned by an AST test that walks each loader and asserts the
declared set EXACTLY matches the keys actually read. Both directions are bugs: a
key read but unlisted REJECTS a valid intent, and a key listed but unread is a
dead allowance that lets the typo through. Mutation-verified in both directions.

**Worth keeping:** the first version of that test hard-coded the accessor helpers
it knew about, missed `_opt_positive_int`, and produced an allow-list that
rejected the shipped default route (`metric: 10`). Caught by the
every-shipped-intent test added the day before, not by review. The test now
DISCOVERS accessors — any `helper(sp, "key", ...)` counts — so a new accessor
cannot silently drop out of the audit.

### Where folder and zone are defined — SETTLED 2026-08-05 (Model A)

They were defined in BOTH `catalog/environments.yaml` and `catalog/apps.yaml`,
and the duplication was not benign: the app's `folder:` was parsed, stored on
`AppDef`, and never read. `_target()` has always used the ENVIRONMENT's folder.

**Decision — Model A:**

| | owner | why |
|---|---|---|
| folder | `environments.yaml` | a rule's folder is a property of the TRAFFIC PATH, not of an endpoint. A rule between apps in two folders traverses both firewalls, so asking an app is ambiguous for most rules. |
| zone | `apps.yaml`, defaulting to the environment pair | genuinely varies per app inside one folder — `web-tier` is `local`, `payments-gateway` is `internet`, same folder |

`app.folder` is removed and now REJECTED, so the shipped files cannot keep
looking authoritative while doing nothing.

**Consequence to keep in view:** "many apps in one environment, in different
folders" is deliberately NOT expressible. If apps must sit in different folders
they belong to different environments — or the rule belongs in the folder they
share, since config inherits DOWN and a common ancestor reaches both.

**Not chosen:** Model B (app owns folder, environment becomes a lifecycle label).
It is coherent but needs a tie-break for source-app-folder != destination-app-folder,
still needs a folder for explicit `cidr:` endpoints that have no app, and makes
app cardinality drive folder cardinality. Worth revisiting only if apps map to
distinct firewalls.

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


### ~~Zone deletion path~~ — TESTED END TO END 2026-08-05, FAILS CLOSED

Run for real against the pilot, both branches, with the zone AND a referencing
rule committed on the device first.

**Referenced zone: SCM refuses at the API. Nothing reaches the firewall.**

```
409 Conflict  API_I00013
"Another entity is currently referencing this object. Therefore operation is
 not possible. Reference: container -> prod-edge -> pre-rulebase -> security
 -> rules -> handmade-refs-dmz -> from"
   type: NON_ZERO_REFS
```

Terraform's destroy fails loudly, the zone survives intact in SCM and on the
device, the resource stays in state, and a subsequent push is a clean no-op.
**The half-applied candidate config this item was opened about does not happen** —
the delete never gets far enough to be committed, because SCM rejects it before
the device is involved. The error even names the exact referencing path, which is
more than the platform's own checks could give.

That matters because `check_zone_consistency` covers rules IN GIT only; a rule
added by hand is invisible to it, by construction. SCM is the backstop for the
case the compiler cannot see, and it holds.

**Unreferenced zone: deletes cleanly, and the interface survives unzoned.**

```
before:  ethernet1/2  17  1  dmz   N/A  0  10.100.1.110/24
after:   ethernet1/2  17  1  <none> N/A  0  10.100.1.110/24
```

The interface keeps its address and loses only its zone membership. PAN-OS drops
traffic on an unzoned interface, so that end state fails closed too — but it IS a
live, addressed interface in no zone, which is worth knowing before deleting a
zone in anger.

**Watch the timing.** The push job reported `success` while the device still had
the zone; it cleared ~90s later. The same false-signal window this project has
hit repeatedly — a green push is not evidence, only the device is.

**Fixed alongside (v1.18.0):** the compiler now deletes tfvars files a compile no
longer produces. Previously the stale `zones.auto.tfvars.json` stayed on disk,
Terraform auto-loaded it, and a real deletion read as `No changes` LOCALLY while
CI (clean checkout, gitignored tfvars) did the right thing. Verification that
lies is worse than none.

**Remaining deletion work is v2.0 (decision 2026-08-05).** Zones are proven and
need nothing further; what is deferred is the DELETION PATH AS A DESIGNED
FEATURE rather than an observed behaviour:

* ~~**`RouteRequest` deletion — untested.**~~ **DONE 2026-08-06, both halves.**

  **Nothing refuses it, at any layer.** SCM destroyed the logical router without
  complaint (a referenced zone returns `409 NON_ZERO_REFS`); the push was
  accepted; the device applied it. No error anywhere.

  **On the device, ~40s after the push job reported success:**

  ```
  before:  0.0.0.0/0  static  10.100.2.1  metric 10  ethernet1/3
  after:   (absent)
  ```

  Connected routes survived (`10.100.2.0/24`, `10.100.3.0/24` and their /32
  locals), and **VRF membership survived** — `ethernet1/3` and `ethernet1/4` kept
  `lr:default` — because destroying the `prod-edge` override reverts to the
  inherited `ngfw-shared` router, which declares the same interfaces and no
  routes.

  So the failure is precisely scoped and precisely silent: **intra-subnet traffic
  keeps working, everything off-subnet is black-holed**, and the config is valid
  at every layer. Restored; route back with age `00:00:28`, proving reinstall.

  This is why the removal classifier tiers it HIGH: it is the only Day-1 kind
  whose deletion produces an outage with no error and no backstop.

* ~~**Uncommitted third-party changes are staged on the pilot.**~~ **WRONG —
  RETRACTED 2026-08-06.** `config-versions/candidate` returns COMMITTED VERSION
  HISTORY, not pending edits. `push.py` says so in its own header, from a
  previous encounter with the same trap: a "detect-drift" guard once refused
  forever because it read that list as pending. Reading it as staged changes
  produced a false claim that other admins had work in the way, and nearly led
  to discarding a candidate that contained nothing of the sort.

  What the data actually shows: `msetiawan`'s version 70 (`rwar`) was COMMITTED
  and the device's RUNNING version is 70, four seconds later. Nothing of anyone
  else's is pending.

  **The real cause of the refused push:** `is_first_push_done: false`. The
  re-onboard reset it, so SCM has no per-admin baseline for the device and
  refuses an admin-scoped partial push — the first push after onboarding must be
  a full one. `device-sync` now reports exactly this state.

* **`device-sync` cannot see an APPLIED-BUT-UNPUSHED change.** Found by using it
  during this test: the router was destroyed in SCM and `device-sync` still
  reported `running=v72 committed=v72`. Terraform writes to SCM's CANDIDATE, and
  only a push creates a version — so there is nothing to compare.

  It catches "committed but not delivered" (device offline during a push), which
  is real but narrower than the header claimed. The missed case is covered
  elsewhere by construction: `apply.yml` pushes immediately after applying, so a
  refused push fails the job loudly. It bites out-of-band applies — a human
  running `terraform apply` by hand, which is exactly how it arose here.

  Closing it needs a candidate-vs-running comparison, and
  `config-versions/candidate` cannot supply one (it is version history).
  **Effort:** M · **Priority:** P2

* ~~**Evidence bundles cover `AccessRequest` ONLY.**~~ **DONE — v1.36.0**
  (schema `fw-evidence/v2`). The bundle is now assembled from the kind registry
  (`kinds.evidence_object`) instead of an explicit `SecurityRule` field list, so
  the shipped tree produces **10 bundles for 10 intents**, up from 5. Three
  things came with it:

  * `request` carries paperwork only — `action` and `environment` moved under
    `compiled`, the same metadata-vs-spec split that `stale_ticket_problems`
    enforces.
  * The path is keyed on SCOPE, so a device-scoped change lands in
    `evidence/device-<serial>/`, mirroring the Terraform roots.
  * The compiled object is serialised WHOLE. The v1 list went stale twice —
    `application`, `profile_group` and `log_setting` were on the compiled rule
    for a release before anyone added them to the bundle, so records claiming to
    be "the effective rule an assessor sees" omitted the threat-inspection
    profile.

  `has_evidence` is gone. It was an honest declaration of a gap, but declaring a
  gap is not the same as it being acceptable, and the flag made the hole look
  like a design.

* ~~**Evidence for a removal.**~~ **DONE — v1.37.0**, ADR-0008 amended
  2026-08-09. A removal TOMBSTONES the object's own record in place
  (`status: removed`, object embedded from the baseline), `removed` means
  destroyed in SCM *and* pushed, and the removal carries its OWN ticket via a
  `Removes: <REQ-id> (TICKET)` trailer — because a deleted intent has nowhere
  left to state one, and without it an August deletion would cite the July
  request that authorised creating the object.

* ~~**The per-kind deletion CONTRACT (an ADR).**~~ **DONE — ADR-0008.** States
  what removal means per kind from MEASURED behaviour, not prediction, and
  decides the stance: **the platform guarantees a VISIBLE deletion, not a SAFE
  one.** The only thing that ever refused a deletion was SCM's reference check,
  which exists for referenced objects and nothing else — incidental protection,
  not designed. Claiming safety would be claiming a control that does not exist.

  Also decides that a NEW KIND must have its removal behaviour measured before it
  ships; until then its removals are CRITICAL. So `NatRequest` removals are
  CRITICAL by default — the rule working, not an oversight.

**Priority:** P2 (v2.0), alongside `NatRequest` and rule ordering — all three are
compiler/intent-model work rather than plumbing.

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

### ZoneRequest `interfaces:` takes a raw string, not a role

**Found while putting a zone on hardware (2026-08-05).** `InterfaceRequest`
names a ROLE (`interface: dmz`) and the catalog resolves it per scope, because
the same interface is `$eth-dmz` at folder scope and `ethernet1/2` at device
scope (ADR-0005). `ZoneRequest.spec.interfaces` is still a plain list of
strings, so `REQ-2026-0806` has to hardcode `$eth-dmz` — the exact
scope-specific literal ADR-0005 exists to keep out of intents.

It is not cosmetic. Probed live: a folder-scope zone binding the literal
`ethernet1/2` is REFUSED —

```
zone -> network -> layer3 'ethernet1/2' is not a valid reference
```

— so the wrong-but-plausible value compiles clean and dies at apply. SCM fails
closed, which is the good outcome; the platform should fail EARLIER. Note
`tests/test_cli.py` uses `interfaces: [ethernet1/2]` as a fixture, i.e. a value
that cannot work on a real tenant.

**Fix:** resolve `interfaces` through `InterfaceCatalog` like `InterfaceRequest`
does, and reject a raw `$`/port literal.

**Effort:** M — schema change, touches the zone loader, compiler and fixtures.
**Priority:** P2

### ~~A greenfield folder has no `$eth-*` variables~~ — BUILT v1.19.0/v1.20.0

Interface variables are now declared in `catalog/interfaces.yaml` (`create_in:`)
and materialised by `fwgitops folder-interfaces` into the folder's CI-owned root.
`$eth-dmz` moved out of `bootstrap-scm-folder` by `state rm` + `import` — a live
zone binds it, so destroy-and-recreate would have taken `dmz` off the firewall.

The reason it moved was CADENCE, not ownership: bootstrap is run-once with local
gitignored state, so filing an ongoing activity there made every later interface
addition a manual apply from one machine, outside the pipeline. See ADR-0005's
amendment.

**ROOT SCAFFOLDING — BUILT v1.20.0.** `fwgitops scaffold-root --folder <name>`
writes `variables.tf`, `main.tf`, `backend.tf` and `backend.hcl.example` for a
new scope. `variables.tf` is GENERATED FROM THE MODULE rather than copied, which
is the point: a root must mirror the module attribute for attribute, because
Terraform discards an undeclared object attribute at the module boundary
silently (ADR-0004, HOLE 3), so a drifted root does not fail — it quietly stops
delivering part of every intent.

`--check` (in CI) and `--sync` close the other half. Adding a module variable
used to break every root by hand; on 2026-08-05 the module gained
`folder_interfaces` and the device root failed the contract test. The tests
DETECT that, generation PREVENTS it, and the tests were deliberately left
untouched — a generator marking its own homework is worth little.

The provider pin is read from the module's `versions.tf`, because roots and
module drifting apart has broken CI here before (roots on `1.0.12-beta.4`, module
on `~> 1.0`, which cannot even select a pre-release).

`main.tf` is written ONCE and never regenerated: it carries hand-written
reasoning, and a root's backend points at real state, so silently rewriting one
is how a state file gets orphaned.

**So greenfield is now:** create the folder in `bootstrap-scm-folder` (it must
precede the firewall's `dgname` registration), `scaffold-root`, add the folder's
roles to `catalog/interfaces.yaml`, open a PR. Everything after the bootstrap is
pipeline-owned.

### ~~Verify the catalog against SCM~~ — BUILT v1.21.0

`fwgitops verify-catalog` (read-only) compares `catalog/folders.yaml` and
`catalog/interfaces.yaml` against `GET /config/setup/v1/folders`. Wired into the
PR gate and the scheduled drift job.

Catches all four shapes this has actually taken or could take:

| divergence | why it matters |
|---|---|
| declared, ABSENT from SCM | the 2026-08-05 case — 3662 |
| declared as a FOLDER, SCM says `on-prem` | the v1.11.0 case — `folder=<serial>` is rejected on write |
| folder under a different PARENT | config inherits down the tree, so the recorded blast radius is wrong |
| firewall under a different parent | its zones/routes/rules come from a folder this repo is not managing |

**`targetable: false` is treated as an acknowledgement, not a failure.** A stale
entry the operator has already fenced off is reported and exits 0. Failing anyway
would train people to ignore the check, which is how a real divergence gets waved
through.

**Objects in SCM the catalog does not mention are NOT reported.** Prisma Access
built-ins are deliberately absent, and a check that cries wolf every run is one
nobody reads.

**Verified against the live tenant by mutation:** restoring
`catalog/folders.yaml` to its state before the 2026-08-05 hand-patch (3662 marked
targetable) makes it exit 2 and name all three stale entries. It would have
caught the bug that motivated it.

Fails closed on an empty read and on a transport failure — a check that passes
when it could not reach the thing it checks is worse than no check.

**3662 REMOVED from both catalogs (2026-08-05).** `verify-catalog` now reports
zero notes. Leaving a known-stale entry in place would have meant every run
printing something to ignore, which is how a real divergence later gets ignored
too — the entry was the last reason the check was ever noisy.

The `site_specific` marking on the `dmz` role stays. With one firewall left it
changes nothing and looks removable; it earns its keep the moment a SECOND
firewall is added without a DMZ port, when the alternative is a coverage-test
failure or a guessed port. The test asserting the marking is MEANINGFUL is now
conditional on there being more than one firewall to compare — it was vacuous on
a one-firewall estate, and would otherwise have failed simply because the estate
shrank.

### Re-parenting a firewall into a different folder — DEFERRED to v2.0

**What:** move an EXISTING firewall from one folder to another.

**What is already known (2026-08-05), which is most of the design:**

* **Device-scope objects travel; folder-scope objects do not.** Addressed
  interfaces are keyed by serial (`terraform/device-<serial>` is named by serial,
  not folder), so addressing survives a move. Zones, routers, rules and `$eth-*`
  variables are folder objects and do NOT — the firewall simply stops inheriting
  one folder and starts inheriting another.
* **So the hazard is precise: the firewall keeps its IP addresses and loses its
  policy.** An addressed interface in no zone drops traffic (verified in the zone
  deletion test), so it fails closed — but that is an outage, not a safeguard.
* **The order is therefore forced:** build the new folder completely, re-parent,
  verify on the device, and only THEN remove the old folder's objects. Two
  Terraform roots have no reason to pick that order on their own.

**Unverified, and it decides everything:** whether SCM supports re-parenting a
REGISTERED firewall at all. `dgname` is set in `init-cfg` at bootstrap, which is
a registration-time association. Against that, the device is a first-class entry
in `/config/setup/v1/folders` with its own `id` and a `parent` field, and
`scm_folder.parent` is a required, updatable string. Suggestive, not proof — it
needs a probe against a live firewall.

**Effort:** L
**Priority:** P2 (v2.0)
**Depends on:** the catalog-vs-SCM check above — BUILT v1.21.0, so this is
unblocked. A move shows up as a PARENT divergence, which is now a blocking
finding, so an out-of-band re-parent is caught rather than silently believed.

### ~~InterfaceRequest — intent kind #3~~ — DONE v1.8.0

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
**Priority:** DONE v1.8.0 — kept for the scope reasoning, which still holds:
zones are useless without interfaces, and that dependency is now the first link
of the ordering chain built in v1.17.0.
**Depends on:** Probe scm_zone field fidelity — DONE, and the
`scm_ethernet_interface` probe it anticipated was run separately and passed.

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
