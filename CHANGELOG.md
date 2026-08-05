# Changelog

All notable changes to `fwgitops` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.26.0] — 2026-08-05

### RETRACTION: device scope works for every resource

Three spikes concluded that zones, logical routers and security rules were
folder-scope only. **All three were wrong.** The firewall was in a broken
registration state; after it was offboarded and re-onboarded into SCM, every
resource accepts a device-scope write — interface, zone, logical router, address,
tag and security rule. Reproduced three times with readback and cleanup.

`_load_zone_spec` and `_load_route_spec` rejected `device:` on the strength of
that conclusion, so valid intents were being refused at PR time. Both now accept
it. The `allow_device` mechanism is removed rather than left as dead code
carrying a wrong rationale; it can return if a resource is ever shown to be
folder-only, with evidence gathered against a healthy device.

**Why the control did not catch it.** Every probe used `scm_ethernet_interface`
as a positive control and it passed every time — because it was the one resource
still working while the device was broken. "Interface works, zone does not" read
as *resource-specific* when it was *device partially broken*. A positive control
proves the path is alive; it does not prove the environment is healthy, and it is
worth least when the passing case is itself the anomaly.

**The error message was literally true.** `"Device <serial> doesn't exist"` meant
the device was not registered for configuration. It was dismissed as misleading
because the device reported `is_connected: true` and every GET worked — read and
config-write paths do not share that registration.

The three spike READMEs carry retractions at the top with the original text left
intact below, so the reasoning that produced the wrong answer stays legible.

### Also: the re-onboard wiped SCM's device-scope overrides

The firewall's running config was untouched (interfaces still addressed and
zoned), but SCM lost all three device-scope interface objects — `device=` scope
showed only the inherited folder objects with `layer3={}` and no addressing, and
`is_first_push_done` was back to `false`.

**A push in that state would have stripped the IPs off a working firewall.**
`terraform apply` on the device root recreated the overrides first; only then is
a push safe.

- 622 tests.

## [1.25.0] — 2026-08-05

### Unknown `spec:` keys are rejected

The sharper half of the metadata guard, and the reason it was worth doing
separately. `metadata:` is paperwork — a dropped key there costs an audit trail.
`spec:` is FIREWALL BEHAVIOUR, so a dropped key is a rule that does not do what
it says, and looks fine doing it:

```yaml
spec:
  logging: true      # compiled clean, logged nothing — the field is `log`
```

No plan diff, no warning, no failed apply. Just a rule weaker than the one that
was approved.

All four loaders (AccessRequest, Zone, Interface, Route) now reject unknown
fields and name the ones they accept.

### The allow-lists are pinned to the loaders by an AST test

Both directions are bugs, and they are different bugs: a key the loader READS but
that is missing from the list REJECTS a valid intent — blocking legitimate work,
with an error that blames the author. A key listed but never read is a dead
allowance that lets exactly the typo this guard exists to catch straight through.

**This is not hypothetical.** The first version of the extractor hard-coded the
accessor helpers it knew about, missed `_opt_positive_int`, and produced a
`_ROUTE_SPEC_KEYS` without `metric` — which would have rejected the shipped
default route, whose `metric: 10` has always reached the firewall correctly
(confirmed against live SCM). It was caught by the every-shipped-intent test
added in v1.24.0, not by review.

The test now DISCOVERS accessors instead of listing them: any
`helper(sp, "key", ...)` counts, and helpers taking `sp` are followed. A new
accessor cannot drop out of the audit by being unknown to it.

- 622 tests (+6).

## [1.24.0] — 2026-08-05

### Unknown `metadata:` keys are rejected

They were silently ignored. That is how a field stops working with nobody
noticing: a typo like `justifcation:` at least fails, because the required field
then reads as missing — but `tickets:`, or a field retired in a later version,
reads as ACCEPTED and does nothing.

The `expires` retirement made it concrete. Removing that field from the schema
alone would have turned every existing `expires:` into a no-op, which is why it
needed an explicit rejection. This closes the class rather than that instance.

```
metadata: unknown field(s) ['tickets']; expected ['id', 'justification',
'requested', 'requester', 'ticket']. Unknown keys are rejected rather than
ignored — a silently dropped field looks exactly like one that works.
```

`expires` keeps its OWN message naming the device-enforced alternative; folding
it into a generic unknown-key list would throw that away exactly when someone
needs it.

A test pins `_METADATA_KEYS` to the `Metadata` dataclass, because a field added
to one and not the other would make every intent using it fail — with an error
blaming the author rather than the schema. Another loads every shipped intent, so
a validation change that rejects this repo's own tree fails here rather than on
the next PR someone opens.

**Not covered, and filed:** `spec:` blocks still ignore unknown keys. That is the
larger surface — `spec` is where firewall behaviour lives, so a dropped key there
is a rule that does not do what it says (`logging: true` compiles clean and logs
nothing; the field is `log`). Five loaders, each needing its own allowed set
derived from its dataclass, and getting one wrong rejects a currently valid
intent.

### Fixed: `terraform plan` lacked the provider's concurrency guard

`apply.yml` has passed `-parallelism=1` from the start, with a comment that the
scm provider cannot handle concurrent token acquisition. Neither plan step did.

A plan REFRESHES every resource, so a folder with ~19 tag objects plus rules,
zones and routers issues exactly that concurrent burst. It surfaces as an
intermittent `403 Forbidden {"msg":"Access denied"}` reading an `scm_tag` — which
reads as a permissions problem and is not one, and which passes on a re-run, so
it looks like flakiness rather than a missing flag. Caught in CI on this PR;
`pr-validate` and `drift-detect` now match `apply`.

- 616 tests (+5).

## [1.23.0] — 2026-08-05

### `expires` removed from the intent schema entirely

v1.22.0 stopped writing it to the firewall. This removes the field: from
`Metadata`, from `ManagedMeta`, from evidence bundles, and from all four intent
files that set one.

It modelled a lifecycle this platform does not run. The date never reached a
device, no job ever removed an expired rule, and on a Day-1 kind it was parsed
and dropped entirely because evidence bundles are `AccessRequest`-only. A field
that means nothing is worse than a missing one — a reader assumes it means
something, and the evidence bundle asserted a date nobody honoured.

**REJECTED, not ignored.** The loader drops unknown metadata keys silently, so
deleting the field alone would have turned every existing `expires:` into a
no-op — the "compiles clean, does nothing" failure this codebase treats as a bug.
An intent still carrying one now fails at PR time with a message saying why and
pointing at the device-enforced alternative.

A leftover `gitops:expires:` TAG is now ignored rather than parsed. No live rule
carries one, and treating it as malformed would fail closed loudly — turning a
stale label into an incident.

Follow-through in `docs/DESIGN.md`, which promised things that now cannot happen:

- the expiry-auto-rollback section is replaced by a statement that expiry is not
  modelled, and why
- the rollback table's `Expiry` row is gone
- **task T11 (scheduled expiry job) is marked DROPPED** rather than left looking
  merely unstarted
- the classifier signal *"permanent rule (no expiry) above a sensitivity
  threshold"* is dropped — every rule is permanent now, so it would fire on all
  of them and mean nothing

PAN-OS keeps real device-enforced expiry (`scm_security_rule.schedule` →
`scm_schedule.non_recurring`) if the requirement ever returns. Noted with the
catch: a lapsed *allow* stops matching and traffic falls through to whatever is
below it, so it is fail-closed only if rule ordering says so.

- 611 tests (+1).

## [1.22.0] — 2026-08-05

### The expiry tag is no longer written to rules

`metadata.expires` used to become `gitops:expires:<date>` — a real `scm_tag`
object, attached to the rule, pushed to the firewall. It is now not written at
all.

**It is CI expiry, not rule expiry.** Nothing on the device acts on it: PAN-OS
stores the tag and ignores it. So it shipped a date that LOOKED like a control
and was not one, which is the expensive kind of metadata — a reader in the SCM UI
reasonably assumes something enforces it, and the evidence bundle asserts an
expiry nothing honours.

A rule's tags describe what the rule IS. Expiry describes what this pipeline
intends to do with the request later: a property of the request, not of the
firewall object. It stays in the intent YAML and the evidence bundle.

Real device-enforced expiry does exist — `scm_security_rule.schedule` pointing at
an `scm_schedule` with `non_recurring` date ranges. A tag was never going to be
it. Not wired; a separate decision.

`parse_managed_meta` still READS the tag, so rules tagged before this change are
understood rather than treated as malformed — which fails closed loudly and would
turn a cosmetic change into an incident.

Applied to the live pilot: three rules lost the tag, both tag objects are gone
(21 -> 19), and the device confirms zero `gitops:expires` with `managed`, `req`,
`section` and `ticket` all still present.

**Consequence, accepted knowingly:** an expired-rule check can no longer be
answered from live state alone. It must read intent YAML, which says what SHOULD
be there rather than what IS.

### Found doing it: tag removal and tag destruction are unordered

Terraform destroyed the `scm_tag` before updating the rules that referenced it,
because after the change the rule's config no longer references it — the edge
that ordered creation vanishes exactly when destruction needs it. SCM refused
(`409 NON_ZERO_REFS`), so nothing was corrupted, but the apply failed and
`-target` does not help. **This is latent for any tag VALUE change**, e.g. a
corrected ticket number; it has never been hit only because no tag value has ever
changed on a live rule. Filed with the analysis, including why the obvious
`depends_on` fix is the pattern this module already removed once.

- 610 tests (+3).

## [1.21.1] — 2026-08-05

### `007955000893662` removed from both catalogs

It left SCM and was fenced off with `targetable: false` as a stopgap. Now
deleted from `catalog/folders.yaml` and `catalog/interfaces.yaml`, so
`verify-catalog` reports **zero** notes.

That is the point of removing it rather than leaving it acknowledged: a check
that prints a known-stale entry every run is one people stop reading, and this
entry was the only reason it was ever noisy.

The `site_specific` marking on `dmz` stays. With one firewall it changes nothing
and looks removable; it earns its keep when a second firewall arrives without a
DMZ port, where the alternative is a coverage-test failure or a guessed port. The
test asserting that marking is meaningful is now conditional on there being more
than one firewall — it was vacuous on a one-firewall estate and would otherwise
have failed merely because the estate shrank.

## [1.21.0] — 2026-08-05

### `fwgitops verify-catalog` — the catalog can no longer lie quietly

`catalog/folders.yaml` is a hand-maintained mirror of a hierarchy that changes
underneath it. It is declared rather than read live on purpose, so the compiler
and classifier stay pure — but purity buys determinism, not truth, and nothing
was checking the truth.

It had already gone wrong twice, in opposite directions: device serials listed as
targetable child FOLDERS (v1.11.0), and a firewall that left SCM entirely while
the catalog kept listing it as targetable with port mappings (2026-08-05). Both
produce the same failure — an intent that compiles clean and dies at apply,
having passed every compile-time check, because none of them look here.

Read-only, wired into the PR gate and the scheduled drift job. Catches:

- declared but **absent** from SCM
- declared as a FOLDER where SCM reports `on-prem` (a DEVICE) — `folder=<serial>`
  is rejected on write
- a folder under a different **parent** — config inherits down the tree, so the
  blast radius this repo records would be wrong
- a firewall under a different parent — its zones, routes and rules come from a
  folder this repo is not managing

Two judgement calls, both about staying worth reading:

- **`targetable: false` is an acknowledgement, not a failure.** An entry the
  operator has already fenced off is reported and exits 0. Failing anyway trains
  people to ignore the check, which is how a real divergence gets waved through.
- **Objects in SCM the catalog does not mention are not reported.** Prisma Access
  built-ins are deliberately unmanaged; flagging them every run would make this
  noise.

**Verified against the live tenant by mutation:** with `catalog/folders.yaml`
restored to its pre-2026-08-05 state, it exits 2 and names all three stale
entries. It would have caught the bug that motivated it.

Fails closed on an empty read and on a transport failure — a check that passes
when it could not reach what it checks is worse than no check.

This also **unblocks v2.0 re-parenting**: a move surfaces as a parent divergence,
which is now blocking, so an out-of-band re-parent is caught rather than believed.

- 608 tests (+12).

## [1.20.0] — 2026-08-05

### `fwgitops scaffold-root` — the last manual step in greenfield

A new folder needed ~260 lines of `variables.tf` hand-copied with every nested
attribute right. Getting one wrong does not fail: Terraform DISCARDS an
undeclared object attribute at the module boundary silently — no warning, exit 0
(ADR-0004, HOLE 3) — so a drifted root quietly stops delivering part of every
intent.

So `variables.tf` is now GENERATED FROM THE MODULE, types copied verbatim, with
only two deliberate differences: maps default to `{}` so `plan` works for a scope
with nothing compiled into it yet, and `folder` defaults to the scope's SCM
folder.

`--check` (wired into both workflows) and `--sync` close the other half. Adding a
module variable used to break every root by hand — it happened on 2026-08-05,
when the module gained `folder_interfaces`. The contract tests DETECT that;
generation PREVENTS it. **The tests were left untouched on purpose**: they are
the independent verification, and a generator marking its own homework is worth
little.

Details that are load-bearing rather than decorative:

- The provider pin is read from the module's `versions.tf`. Roots and module
  drifting apart has broken CI here before (roots on `1.0.12-beta.4`, module on
  `~> 1.0`, which cannot even *select* a pre-release). A naive `version = "..."`
  search matches `required_version` first and pins the provider to the Terraform
  constraint — a valid-looking string that nothing downstream complains about, so
  the block is scoped and a test asserts the result is not `>= 1.6`.
- A DEVICE root's `folder` variable is its CONTAINING folder, never the serial:
  the module scopes `scm_tag` with it, tags are folder objects even when the
  interface is a device override, and SCM rejects `folder=<serial>` outright.
- `main.tf` is written ONCE, never regenerated. It carries hand-written
  reasoning, and a root's backend points at real state — silently rewriting one
  is how a state file gets orphaned.
- `default` inside `optional(...)` is not a variable default. Stripping one would
  truncate a TYPE, which is the exact damage this prevents. Tested.

Verified by scaffolding a real root: `terraform fmt`, `init` and `validate` all
clean, and the contract tests picked it up automatically. Both existing roots
were `--sync`ed and still plan clean against live SCM.

**Greenfield is now:** create the folder in `bootstrap-scm-folder` (it must
precede the firewall's `dgname` registration), `scaffold-root`, add the folder's
roles to `catalog/interfaces.yaml`, open a PR.

- 596 tests (+9).

## [1.19.0] — 2026-08-05

### Folder interface variables move out of bootstrap — greenfield, mostly

A folder-scope zone can only bind an interface object that exists at that scope
(binding a literal port is refused as an invalid reference). `$eth-local` and
`$eth-internet` exist only because they are SCM defaults inherited from
`ngfw-shared`, so a NEW role — or a new folder wanting one — had nothing to bind.

`$eth-dmz` was created by hand in `bootstrap-scm-folder` in v1.18.0. That was
wrong, and the reason is CADENCE, not ownership: bootstrap is run-once with local
`.gitignore`d state, so its state lives on exactly one machine. Every later
interface addition would have been a manual apply from that machine — no PR plan,
no risk classification, no evidence bundle, invisible to drift.

Now: declared in `catalog/interfaces.yaml` under `create_in: {folder: port}` and
materialised by **`fwgitops folder-interfaces`** into the folder's CI-owned root,
sharing the remote state its zones and rules already use. The permission boundary
is unchanged — the catalog is platform-maintained and changed by PR, so a
requester still cannot conjure a port by filing an intent.

`$eth-dmz` moved by `state rm` + `import`, NOT destroy-and-recreate: a live zone
binds it, and destroying it would have taken `dmz` off the firewall. The only
resulting diff was the management comment.

Two constraints fall out of the data model, both fail-closed:

- **Opt-in, never inferred.** Listing an inherited SCM default under `create_in`
  would SHADOW the shared object with a per-folder copy, silently re-pointing the
  interface every firewall in that folder resolves that role through. A test
  forbids `local` and `internet` appearing there.
- **One folder variable cannot be two ports.** If a folder's own firewalls map
  the role differently, `folder-interfaces` reports and writes NOTHING rather
  than picking one — choosing would send the other firewall's traffic out the
  wrong wire while every check stayed green.

### `007955000893662` is gone from SCM, and the catalog did not notice

It vanished from `GET /config/setup/v1/folders`; `catalog/folders.yaml` went on
listing it as targetable with port mappings. An intent naming it compiled clean
and would have died at apply. Marked `targetable: false` as a fail-closed
stopgap — but nothing DETECTS the next one, and a catalog-vs-SCM check is now
filed as the prerequisite for folder moves (v2.0), since re-parenting is exactly
this kind of hierarchy change.

Two tests silently depended on that firewall being targetable and were rewritten
against synthetic fixtures — they asserted properties of the loader, so they
should never have leaned on the shipped catalog to stay in a particular state.

### Also

- The module merges `folder_interfaces` with `interfaces` into one `for_each`.
  They are separate VARIABLES because both arrive as auto-loaded tfvars and
  Terraform REPLACES a variable set twice rather than merging it — one name would
  let whichever file loads last erase the other, with no diagnostic. The merge is
  safe because folder variables are `$`-prefixed and device interfaces are not;
  `folder-interfaces` asserts that rather than assuming it.
- The root/module contract tests caught the first attempt (which merged in the
  root) and were left untouched — the wiring was wrong, not the guard.
- `default_value` is now wired in the module; it was previously unwired, so a
  folder variable would have been created pointing at no port.
- **Not closed:** a brand-new folder still needs its Terraform root scaffolded
  before `compile` emits into it. Greenfield is closer, not finished.
- 587 tests (+9).

## [1.18.0] — 2026-08-05

### ZoneRequest reached a firewall — the last unverified Day-1 kind

Built since v1.2.0, never once seen on hardware: this tenant's zones pre-exist in
`ngfw-shared` and self-attach, so every green apply proved the CHAIN, not the KIND.
Zone `dmz` is a name that existed nowhere on the tenant, so its presence in the
device's pushed config is attributable to GitOps and nothing else.

```
ethernet1/2   17   1   dmz   N/A   0   10.100.1.110/24

dmz { network { layer3 ethernet1/2;
                log-setting log-best;
                zone-protection-profile best-practice; } }
```

Full field fidelity, and the `$eth-dmz` variable resolved to the physical port on
the way down.

### Zones are folder-scope only — `device:` now rejected at PR time

`spike/zone-device-scope`. SCM refuses a device-scope zone with "Device <serial>
doesn't exist" while the SAME device-scope write of an ethernet interface on the
SAME firewall succeeds — the control that makes this resource-specific rather
than a property of the device. The provider documents `device` for `scm_zone`;
the API disagrees. NOT a provider defect: the identical raw REST call fails
identically with no Terraform involved.

Second of four Day-1 resources to behave this way (after `scm_logical_router`),
so folder-only is now the default assumption for a network resource.

### `site_specific` interface roles

`dmz` exists on one firewall, not both. The coverage test asserted every role
maps every targetable firewall — right for `local`/`internet`, wrong for a port
that is one site's wiring. A role may now declare `site_specific: true`, which
changes what the test EXPECTS and nothing about what is ENFORCED: `resolve()`
still fails closed for an unmapped firewall, and a test asserts exactly that.

### Zone deletion tested end to end — it fails closed

Run for real against the pilot, with the zone AND a referencing rule committed on
the device first. Deleting a REFERENCED zone is refused by SCM at the API:

```
409  NON_ZERO_REFS
"Another entity is currently referencing this object ... Reference:
 container -> prod-edge -> pre-rulebase -> security -> rules
 -> handmade-refs-dmz -> from"
```

The destroy fails loudly, the zone survives in SCM and on the device, and the
half-applied candidate config the TODO feared never happens — the delete never
reaches the firewall. This is the backstop for the case
`check_zone_consistency` cannot see, since it only covers rules in Git.

Unreferenced, it deletes cleanly and the interface survives ADDRESSED BUT
UNZONED. PAN-OS drops traffic on an unzoned interface, so that fails closed too.

The push job reported `success` ~90s before the device reflected it — the same
false-signal window as ever.

### Compile now deletes tfvars it no longer produces

Removing the last intent of a kind left the previous `*.auto.tfvars.json` on
disk; Terraform auto-loaded it and silently re-asserted the deleted object. CI
was always correct (files are gitignored, clean checkout), so this only bit
someone verifying a deletion locally — with a confident wrong answer. Only a
registered kind's exact filename is removed; a hand-maintained file is left
alone.

### Also

- `$eth-dmz` declared in `terraform/bootstrap-scm-folder` — a folder-scope zone
  can only bind a variable that exists at that scope, and no Day-1 kind creates
  one. Boundary recorded in TODOS.
- `RouteRequest` deletion is still untested, and a route is what an outage runs
  through — the same experiment is worth repeating there.
- 578 tests (+7).

## [1.17.0] — 2026-08-04

Cross-kind ordering — the last unbuilt piece of ADR-0002.

### Declared in the registry, not hard-coded
`KindHandler.depends_on_kinds` states each kind's requirements, and
`kind_apply_order()` topologically sorts them, tie-broken alphabetically so two
runs agree. A build that is not reproducible is not ordered, it is lucky.

```
InterfaceRequest -> ZoneRequest -> RouteRequest -> AccessRequest
```

### It exists because the chain SPANS ROOTS
Inside one root Terraform orders by resource reference and does it better —
`scm_security_rule` already references `scm_zone.this[z].name`. But interfaces
are DEVICE-scoped while zones, routes and rules are FOLDER-scoped, so they live
in separate states no single graph covers. That is the whole gap this fills.

### `fwgitops apply-order`
Prints Terraform roots in dependency order; the apply workflow consumes it.
Previously it ran `for dir in terraform/*/` — alphabetical. On this tenant
`device-<serial>` sorts before `prod-edge`, so interfaces happened to apply
before what depends on them: **correct by accident**, and it would have inverted
silently on a rename.

### Fails closed, three ways
A dependency cycle; a dependency naming an unregistered kind; and kinds
**interleaved across roots** such that no whole-root order satisfies them — that
last needs per-kind applies, so it exits 2 naming the conflict rather than
picking a sequence that looks like success.

`fwgitops kinds --order` exposes the chain for scripting. The default remains
alphabetical, unchanged.

571 tests (+10).

## [1.16.0] — 2026-08-04

`enrich` narrows to ORDERING only. Terraform owns the fields.

### One writer per field
v1.15.0 wired `application`, `profile_setting`, `log_setting`, `source_user`,
`category`, `negate_*`, `log_start` and `description` into the module, on
provider 1.0.12-beta.4. `enrich` was still writing all of them too.

Two writers for one field is the ambiguity that produced the `profile_setting`
P1 in the first place. And the redundant write is not harmless: it would
**silently repair a field Terraform failed to set**, so a regression in the
module would never surface — the pipeline would stay green while the mechanism
that is supposed to be authoritative had stopped working.

Pass 1 of `enrich_folder` no longer PUTs. `_merged_body` is deleted rather than
left as authoritative-looking dead code.

### What stays, and why it cannot move
Before/after ordering. An anchored move needs the anchor's UUID —
`scm_security_rule.this[<key>].id` — which inside a single `for_each` block is a
self-reference Terraform rejects with `Error: Cycle`. A Terraform limitation,
not a provider one: the SCM move endpoint works when given the UUID.

`top` / `bottom` need no anchor and are honoured through `relative_position`.

### Kept deliberately
The fail-closed existence check. `enrich` must never run against a folder whose
skeleton did not apply, and that guard is independent of what it writes.

561 tests — six that asserted `enrich` PUTs those fields are replaced by three
asserting it does NOT, that it still orders, and that it still fails closed.

## [1.15.0] — 2026-08-04

Adopts scm provider **1.0.12-beta.4**, which writes the security-rule fields
1.0.11 silently dropped — closing a live security gap.

### The gap
Every `prod-edge` plan wanted to change five rules with no pending intent
change. `source_user` is optional-NOT-computed, so absent config meant REMOVE,
and `REQ-2026-07302` carried `profile_setting={group: [best-practice]}`. An
untargeted apply would have cleared it: the rule keeps allowing `ssl`/
`web-browsing` outbound and **stops inspecting it**. The plan called that
`(known after apply)`.

### The fix came from the provider's own example
It sets `category = ["any"]` and `source_user = ["any"]` EXPLICITLY. Omission is
not "leave alone". The intent model already carried both; the compiler simply
did not emit them.

Now wired: `log_setting`, `profile_setting`, `description`, `log_start`,
`source_user`, `category`, `negate_source`, `negate_destination`. `prod-edge`
plans clean — `0 to change` on the rules, verified on the device that
`best-practice` and both log profiles survived.

### Pinned EXACTLY, because it is a pre-release
`version = "1.0.12-beta.4"`, not `~> 1.0`. A floating constraint would drift off
a pre-release in either direction without anyone choosing to.

### Still with `enrich`, and why
Before/after ordering. An anchored move needs `target_rule` as the anchor's
UUID, which inside one `for_each` block is `scm_security_rule.this[<key>].id` —
a self-reference Terraform rejects with `Error: Cycle`. `enrich` resolves it
over REST after apply.

`position` / `relative_position` are honoured by the provider but deliberately
NOT wired yet: the compiler defaults every rule to `bottom`, so wiring them
would move five live rules at once, and rule order is policy. Needs its own
probe of "move an EXISTING rule".

564 tests.

## [1.14.0] — 2026-08-03

`interface:` names a ROLE, resolved per scope by `catalog/interfaces.yaml`.

### A port number does not belong in an intent
The same interface has two names in SCM and which is correct depends on the
scope being written (ADR-0005):

```
folder=ngfw-shared   $eth-local (default_value: ethernet1/4)   35479f59-…
device=<serial>      ethernet1/4                               35479f59-…
```

An intent hardcoding either is wrong somewhere: `ethernet1/4` breaks when it
targets a folder, `$eth-local` breaks when it targets a firewall. Worse, the
physical name is a property of the AWS topology — it changed during this
release and would have silently invalidated every intent naming it.

So an intent now says `interface: local` and the catalog resolves the role for
whichever scope it targets, at LOAD time, exactly as `catalog/routers.yaml`
supplies VRF membership. The compiler stays pure and the requester never types
a port.

Fail closed throughout: an unknown role is rejected with the known ones listed,
a role with no mapping for the target firewall is rejected rather than guessed,
and a **missing** catalog makes `interface:` unusable rather than falling back
to the literal — that fallback would have been the hardcoded-port problem
wearing a catalog.

### The mapping is the cost driver
Recorded in the catalog itself. Naming `ethernet1/3` and `ethernet1/4` forces
ENIs at device index 3 and 4, hence a 5th ENI, hence a 16-vCPU instance — which
also auto-scaled the licence from `VM-SERIES-4` to `VM-SERIES-16`. Mapping the
same roles to `ethernet1/1` / `ethernet1/2` would fit 1 mgmt + 2 dataplane on
`m5.xlarge`, which is what a production firewall here actually needs.

Changing that is now a catalog edit rather than a rewrite of every intent —
which is the point.

564 tests.

## [1.13.0] — 2026-08-03

`fwgitops push --device <serial>` — and the **first GitOps-managed data-plane
change applied and pushed to live hardware**.

### Pushing a firewall
pan.dev documents `devices` on the push body ("The target devices for the
configuration push") alongside `folder`. A device-scope override belongs to the
firewall, so pushing its folder would be the wrong instrument — it would commit
whatever else is staged there rather than the one change intended. Exactly one
of `<folder>` / `--device`, and the wire shape is asserted in a test because a
wrong key would fail as a silent no-op push.

### First hardware change
`intent/prod/edge-fw-4453/REQ-2026-0801.yaml` — `ethernet1/4` on
fw-prod-edge-4453 addressed `10.20.0.1/24`, targeted with `device:`, classified
**HIGH `interface_becomes_addressed`**, applied and pushed (job 126,
`CommitAndPush` FIN/OK, "Configuration committed successfully").

Isolation held exactly as `spike/device-override-probe` predicted: the firewall
got a new override object, while the other firewall and `ngfw-shared` were
byte-identical to a baseline captured before the apply. `terraform plan` reports
converged.

The existing inherited zone `local` already references `$eth-local`, so no
`ZoneRequest` was needed — the Day-1 gap on this tenant is addressing and routes,
not zones.

### Known gap found doing it
Pushing a device with nothing staged returns a normal success job rather than the
no-op SCM reports elsewhere, so `status="noop"` is unreachable on this path and
repeat pushes mint empty commit jobs. Tracked in `TODOS.md` (P2).

557 tests.

## [1.12.1] — 2026-08-03

**Fixes a fail-open the v1.12.0 Scope change introduced.** Found while preparing
the first hardware change, before it was applied.

### A device-scoped change was reported LOW with no checks
`interface_becomes_addressed` (HIGH) distinguishes "puts the interface on a
network" from "edits an existing address" by looking it up in live state. It
keyed on `.folder`, which is **None** for a device-scoped object — so the lookup
always missed, the check never fired, and putting a production firewall's
interface on a network for the first time classified LOW.

v1.12.0 moved grouping, drift keys and the output path onto `Scope` and left the
classifier keying on `.folder`. All three now build the same key
(`_scope_key` == `Scope.key`), asserted by a test, because that is the seam where
a silent miss is invisible.

### `fwgitops snapshot --device <serial>`
Without it the classifier had nothing to compare a firewall's objects against, so
the check could never fire on real hardware regardless of the fix above. Rows are
stamped `scope: device:<serial>`, matching what drift and the classifier look up
by. Exactly one of `<folder>` or `--device` is required.

### Folder fan-out is deliberately skipped for a firewall
Targeting one firewall is the narrowest act available — a device write creates a
per-device override and reaches nothing else. `_blast_radius` now returns nothing
for a device scope on purpose, rather than by accident via `folder=None`.

Verified against the live tenant: `classify` reports the first hardware change
`HIGH interface_becomes_addressed`, where before the fix it read `LOW -`.

554 tests.

## [1.12.0] — 2026-08-02

`device:` targeting — a Day-1 intent can name **one firewall**. ADR-0006
addendum.

### The firewall is the last level of the hierarchy, addressed differently
In SCM config inherits `All → ngfw-shared → prod-edge → <firewall>`, so a
firewall IS a hierarchy member — but it is addressed `device=<serial>`, never
`folder=<serial>`. v1.11.0 conflated the two; v1.11.1 over-corrected by deleting
firewalls from the hierarchy. Both halves are now modelled: `catalog/folders.yaml`
lists firewalls under `devices:` beneath their folder, and the Day-1 kinds take
**exactly one** of `folder:` / `device:` / `environment:`.

Putting a serial in `folder:` — or a folder in `device:` — is rejected with a
message naming the right field, so the v1.11.0 mistake is caught at the
requester's door rather than at apply.

### A firewall gets its own Terraform root
Probed, not assumed (`spike/device-override-probe`): a device-scope write to an
inherited object creates a **per-device override** with its own id, leaving the
shared object and the other firewall untouched. Two objects means two states, so
a `device:` intent compiles to `terraform/device-<serial>/`. Sharing a root would
let one scope's plan destroy the other's overrides.

Compiled objects now carry a `Scope` (folder **or** device) rather than a bare
folder string; grouping, the output path, drift keys and `routers.yaml` keys all
follow it.

### `tests/test_tfroots.py`
Every Terraform root is checked against the module: same variables, declared
**structurally** identically (attribute paths via `declared_object_attributes`,
so comments may differ but types may not), and every variable wired into the
module call. The roots are copies, and that duplication is where HOLE 3 returns —
`prod-edge`'s own `security_rules` block carries a comment recording exactly that
happening. Mutation-proven against both a dropped attribute and an unwired
variable, and it covers a new root the moment one exists.

549 tests.

## [1.11.1] — 2026-08-02

**Corrects a wrong claim shipped in v1.11.0.** A device is not a folder.

### Device entries removed from `catalog/folders.yaml`
`GET /config/setup/v1/folders` returns two kinds of entry, told apart by `type`:
`container` is a real folder, `on-prem` is a **device** (carrying
`serial_number` and `model`). v1.11.0 read the two `on-prem` entries parented to
`prod-edge` as per-device folders, listed them as children, and marked them
`targetable: true`.

They are not folders:

* `folder=007955000894453` → **400 API_I00013, "Folder … doesn't exist"**
* the same serial works as `device=`, returning `ethernet1/3` / `ethernet1/4`
* pan.dev documents `folder` / `snippet` / `device` as three separate query
  params; the Terraform provider says "exactly one of" on every resource

An intent naming a serial **compiled clean and would have failed at apply**. It
is now rejected at compile time, and a test asserts device serials stay out of
the catalog.

### v1.11.0's "blast radius fix" was itself the bug
That release claimed to fix an understated blast radius by giving `prod-edge`
two child folders. The original `children: []` was correct — `prod-edge` has no
child containers. Its two firewalls are devices attached to it, and a change
there reaching both of them is the folder's purpose, not a hidden fan-out. The
catalog and its test are restored.

### Still unsolved: targeting one firewall
It needs a `device:` scope. The resources support it (`scm_zone`,
`scm_ethernet_interface`, `scm_logical_router` all take `folder`/`snippet`/
`device`); this platform does not implement it. A design decision, not a catalog
entry.

Everything else in v1.11.0 stands: `folder:` on the Day-1 kinds, `environment:`
on `AccessRequest`, exactly one of the two, targetability enforced at compile
time, and the `AccessRequest`-ignores-`folder:` fix.

540 tests.

## [1.11.0] — 2026-08-02

The Day-1 kinds now name their target folder directly. See ADR-0006.

### `folder:` on InterfaceRequest / ZoneRequest / RouteRequest
`environment:` resolves 1:1 to a folder, so it could not name a DEVICE folder at
all — and SCM parents one under `prod-edge` per onboarded firewall, which is the
tightest scope that still reaches real hardware. Worse, the only way to express a
new target was to edit `catalog/environments.yaml`, turning a one-off targeted
change into a platform-config PR.

`AccessRequest` is unchanged and keeps `environment:` — app teams should never
need to know SCM topology. Different author, different addressing; ADR-0001's
principle is that kinds declare capability rather than fake uniformity.

Day-1 kinds take **exactly one** of `folder:` / `environment:`.

### Guarded by the catalog, not the classifier
`catalog/folders.yaml` gains `targetable: true|false`. An intent naming an
unknown or non-targetable folder is **rejected at compile time** — not tiered up.
The `folder_with_children` check still fires HIGH, but HIGH is *approvable*, and
a write to a shared parent like `ngfw-shared` should not be one rubber-stamp away
from reaching every device at once. Fail closed throughout: unknown folders are
not targetable, and a **missing** catalog makes `folder:` unusable rather than
unchecked.

### ~~Fixed: the shipped folder hierarchy understated production blast radius~~
> **RETRACTED in v1.11.1.** This "fix" was itself the bug — the entries taken for
> per-device folders are `on-prem` DEVICE entries, and `prod-edge: children: []`
> was correct all along. See v1.11.1 above.

`catalog/folders.yaml` declared `prod-edge: children: []` while SCM has had two
device folders under it all along. The classifier reads that file, so **changes
to the production folder scored as reaching no descendants when they in fact
reach both firewalls.** Understating blast radius on the production folder is the
worst direction to be wrong in. A test asserted the stale shape; it now asserts
the live one.

### Fixed: `AccessRequest` silently ignored `folder:`
It was an unknown key, so an AccessRequest that copied `folder:` from a Day-1
example landed in whatever `environment` resolved to while its author believed
otherwise — a silently wrong target. Now rejected with a message naming
`environment:`.

538 tests.

## [1.10.0] — 2026-08-02

`RouteRequest` (ADR-0001 kind #4) closes ADR-0002's ordered Day-1 chain:
`InterfaceRequest → ZoneRequest → RouteRequest → AccessRequest` is now
expressible end to end.

### HOLE 3 was passing VACUOUSLY for every list-of-object attribute
Found while building this kind, and the more important half of the release.
`_emitted_paths` recursed into dicts but **not into lists**, so a
`list(object(...))` attribute contributed only its own path and nothing beneath
it was ever compared against the declaration. For `routers` the check looked at
`{name, folder, vrf}` and **never once at a static-route field** — four levels of
type that Terraform would have discarded silently, which is precisely the hole
the check exists to close. It also applied to `interfaces` as shipped in v1.8.0.

`declared_object_attributes` collapses the list level (`vrf.name`, not
`vrf.*.name`), so the emitted side now collapses it too and the two line up.
Mutation-proven: deleting `admin_dist` four levels deep from the declaration is
caught. Re-run against the shipped kinds, `zones` and `interfaces` are clean —
the gap hid no live defect, but it was hiding it by not looking.

### One intent per route; the compiler aggregates
Unlike every other kind, one intent does **not** map to one object. A static
route lives at `vrf[].routing_table.ip.static_route[]` inside
`scm_logical_router`, and that same object also carries the VRF's **interface
membership**. Terraform manages whole objects, so writing a router without that
membership would strip the interface list off the object every packet traverses.

`catalog/routers.yaml` declares membership; it is resolved at **load** time into
`RouteSpec.vrf_interfaces`, so the compiler stays pure (no live reads — the same
intents always compile to the same output). `route_tfvars` re-asserts it and
rejects folder-spanning routers, disagreeing membership, and duplicate route ids.

### Risk
`classify_route` adds `default_route` (HIGH — a `0.0.0.0/0` route decides where
all unmatched traffic goes) and `router_becomes_locally_owned` (HIGH — the first
route against an inherited router creates a local override, moving ownership).
Routes are the first kind whose failure mode is an **outage** rather than a
no-op.

### Provider fidelity: probed, faithful
`scm_logical_router` was probed against the live tenant before this kind was
declared safe to apply — created in `GitOps` (zero devices, nothing inherits),
read back over the SCM API, destroyed. **All seven checked paths honored, four
levels deep**, and a re-plan showed no phantom diff (which is how the
`scm_security_rule` problem first surfaced). So `RouteRequest` needs no `enrich`
subsystem. Kit and results: `spike/router-probe/`.

The record is four for four at catching what inference would have got wrong or
merely guessed — `scm_security_rule` drops fields; `scm_zone`,
`scm_ethernet_interface` and `scm_logical_router` do not. Fidelity is per
resource type: probe before building the next kind, do not extrapolate.

524 tests.

## [1.9.0] — 2026-08-02

Closes the gap v1.8.0 shipped with: the registry declared `drift_engine="state"`
for `InterfaceRequest`, but the drift engine only knew about zones. **The
registry made a claim the code did not keep.**

### State drift is registry-driven
`declared_zone_state` → `declared_state(handler, objs)`, built from the kind's
own registered `tfvars` emitter. A kind declaring `drift_engine="state"` is
covered the moment it registers — no per-kind function, and the drift comparison
cannot disagree with what Terraform applies about what an object should look
like.

`fwgitops drift` takes `--state-snapshot` (repeatable) in place of
`--zones-snapshot`, and reports per kind.

### Snapshots stamp their kind
`fwgitops snapshot` records `kind` on every row, and drift **refuses** a snapshot
without it rather than guessing — mis-attributing a snapshot would compare it
against the wrong declared set entirely.

### `fwgitops kinds`
Lists registered kinds, `--state-drift` for those that cannot carry tags. Lets
`drift-detect.yml` enumerate them, so **adding a kind needs no workflow edit**.

**Tests: 491 → 496.**

## [1.8.0] — 2026-08-02

**`InterfaceRequest` — intent kind #3**, and the first kind added since the
registry landed. Designed in ADR-0005, built once all four of its prerequisites
were met.

### What it does
CONFIGURES an existing interface rather than creating one. On the pilot tenant
the interfaces already exist as folder-scope variables (`$eth-local`) with
`layer3` empty on every one — what an `InterfaceRequest` supplies is the
addressing.

```yaml
kind: InterfaceRequest
spec:
  environment: prod
  interface: "$eth-local"
  ip: ["10.20.0.1/24"]     # or: dhcp: true — exactly one
  mtu: 1500
```

Exactly one addressing mode is required. The provider says so, and the loader
rejects violations at PR time rather than letting the device commit do it.

### The registry paid off
Adding this kind was **one `REGISTRY` entry** driving compile, tfvars emission,
classification, report labelling and snapshotting — plus a Terraform variable and
resource. It is the first kind not wired by hand into eight places.

`run_classify` and the snapshot command are now generic over the registry, so a
future kind is classified and snapshottable the moment it is registered.
`snapshot-zones` becomes `snapshot <kind> <folder>`; `classify --zones-snapshot`
becomes `--state-snapshot` (repeatable).

### Risk
- `interface_becomes_addressed` (HIGH) — assigning addressing where `layer3` was
  empty puts the interface **on a network**. Editing an existing address changes
  something already live. Not the same act.
- `folder_with_children` (HIGH) — shared with every kind. The env map decides
  which folder an interface change targets: `prod` → `prod-edge` is a local
  override affecting production only, while pointing an env at `ngfw-shared`
  reaches the sandbox too and is tiered up accordingly.

### Known gap
`fwgitops drift`'s object engine is still zone-specific, so interfaces have no
drift coverage wired even though the registry declares `drift_engine="state"`.
Tracked in `TODOS.md`.

**Tests: 470 → 491.**

## [1.7.0] — 2026-08-02

Security hardening of the CI path. No functional change.

### Credentials can no longer reach a published artifact or PR comment
`pr-validate` folds terraform's stderr into `plan-*.txt`, uploads it as an
artifact, and pastes it into a PR comment — while `SCM_CLIENT_SECRET` sits in the
job env. **GitHub masks the live log stream but not artifact contents or
`gh pr comment` bodies**, so a provider or auth error echoing a credential would
land somewhere durable and public while the visible log looked clean.

Not observed — structural, and open since the plan-step fix in v1.1.0 introduced
the `2>&1`.

`.github/scripts/redact.py` strips secret values before either publish step, with
`if: always()` because a failing plan is exactly when such an error is most
likely. Literal substring replacement, not regex: a secret can contain any
character.

One test asserts **every secret the workflow injects** appears in `SECRET_VARS`,
so adding a secret to the job env without redacting it fails the suite.

### `spike/zone-probe` refuses production folders
It carried the warning in prose only, while `interface-probe` enforced it in a
`validation` block. Prose is not a guard. Now refuses `prod-edge`,
`ngfw-shared` and `All`; verified all three rejected and `GitOps` accepted.

**Tests: 458 → 470.**

## [1.6.0] — 2026-08-02

**ADR-0001's registry promise, finally kept.** Adding an intent kind is now one
registry entry instead of ~8 hand-edited sites.

### `fwgitops.kinds.REGISTRY`
Each kind registers a `KindHandler` carrying its compile function, tfvars
filename and emitter, folder/name accessors and classifier. Replaces:

- `compile_any`'s isinstance chain (moved out of `compiler.py`, which now owns
  the per-kind compile *functions* while the registry owns choosing between them)
- **eleven** isinstance filters in `cli.py`
- a hand-written tfvars emission block per kind — now one loop over the registry

If a kind's Terraform side is missing, the compile fails closed (ADR-0004)
instead of emitting data nothing reads. That is the failure `ZoneRequest` shipped
with for an entire release.

### What it deliberately does NOT unify
A protocol with optional members for stages a kind cannot support would be an
interface with holes. Two stages are genuinely not uniform, so capability is
**declared**:

| Field | Why |
|---|---|
| `drift_engine` | `"tag"` for rules (they carry `gitops:` provenance), `"state"` for zones (`scm_zone` has no `tag` attribute). Same word, different mechanism. |
| `has_evidence` | `build_bundle` is rule-shaped; there is no kind-agnostic bundle today. |

### Tests
New `tests/test_kinds.py` asserts the registry is **complete and
self-consistent**, not merely that dispatch works: every handler fully populated,
kinds matching the intent loaders exactly, tfvars filenames unique, every
filename covered by the gitignore glob, compiled types distinct and
non-overlapping. Verified by mutation — registering a kind under the wrong name
fails five of them.

**Tests: 442 → 458.**

## [1.5.0] — 2026-08-02

ADR-0005's prerequisite 2, generalised beyond interfaces and shipped exercised
on a kind that exists today.

### New check — `zone_becomes_traffic_bearing` (HIGH)
Populating a previously-empty security-relevant field is **not the same act** as
editing a populated one. Assigning an IP to an unaddressed interface puts it on a
network; binding an interface to an empty zone starts carrying traffic through
it. Editing either changes something already live.

This is not hypothetical: **four of the seven zones on the pilot tenant sit at
`layer3: []`** — the normal state, not an edge case. A change moving one out of
it alters what the firewall passes, and now will not auto-apply at a LOW gate.

`_becomes_populated` is the shared helper; interface addressing plugs into it
when `InterfaceRequest` lands.

### `fwgitops classify --zones-snapshot`
State-aware checks need current state, so `classify` accepts the snapshot
produced by `snapshot-zones`. Absent snapshot **disables** those checks rather
than guessing — the classifier says what it can prove.

Keys on the snapshot's `scope` (the folder QUERIED), not the folder SCM reports
an object as defined in. Getting that backwards would mean an inherited zone
never matches its declaration and the check silently never fires.

**Tests: 434 → 441.**

## [1.4.0] — 2026-08-02

Two of ADR-0005's four blocking prerequisites for `InterfaceRequest`. Both close
gaps that exist today rather than only mattering later.

### HOLE 3 now applies at any depth
The object-attribute check inspected only the TOP level of an `object({...})`
type — a documented limitation. Terraform discards an undeclared attribute at
**any** depth, and both `network` (zones) and `layer3` (interfaces) are nested,
so a root whose nested type was narrower than the module's would drop fields
while the top-level key looked perfectly wired.

The check now recurses and compares dotted paths
(`network.zone_protection_profile`). A `null` nested object asserts nothing about
its children, so an unset `optional(object(...))` is not a false positive.

### New check — `folder_with_children` (HIGH)
A change scoped to a folder that has child folders reaches every one of them. On
this tenant `ngfw-shared` parents both `prod-edge` (production) and `GitOps`
(sandbox), so one change there lands on both — the largest blast radius this
platform can produce.

Driven by a new `catalog/folders.yaml`. The classifier stays **pure**: the
hierarchy is declared config, not a live SCM read. Applies to every kind, so an
env map pointing at a parent folder tiers up its *rules* too, not just
interfaces. Absent hierarchy disables the check rather than inventing a verdict.

**Tests: 421 → 434.**

## [1.3.0] — 2026-08-02

**Drift detection now covers objects that cannot carry tags.**

The existing engine keys entirely off `gitops:` tags. That works for security
rules and covers nothing else: `scm_zone` and `scm_ethernet_interface` have no
`tag` attribute, and only **14** of the provider's resources do. Zones — shipped
in v1.2.0 — were invisible to drift detection, in a product whose deliverable is
NIST-mapped compliance evidence.

### New — state-based drift
Without a provenance marker you cannot ask "did *we* create this?". You can still
ask what matters:

| Class | Meaning |
|---|---|
| `UNEXPECTED` | Present in SCM, neither declared nor a known baseline object |
| `MISSING` | Declared in Git, absent from SCM |
| `MODIFIED` | Declared and present, but a field differs |

The `baseline_zones` allowlist (v1.1.0) is what makes `UNEXPECTED` meaningful
rather than noise — it names the objects that legitimately pre-date GitOps.

Only fields the declaration actually **sets** are compared: a `null` means "we
did not ask for this", so SCM's value is not drift. Desired state is built from
the compiler's own tfvars emitter, so drift and what Terraform applies cannot
disagree about what a zone should look like.

### New — `fwgitops snapshot-zones`
Read-only SCM read producing the snapshot, wired into `drift-detect.yml`. This is
the only check that can see a zone **added by hand**: `terraform plan` sees only
changes to resources already in its state, and zones carry no tags.

### Inheritance
SCM returns the folder an object is **defined in**, not the folder queried. Every
zone on the pilot tenant is defined in the shared parent, so keying on the
returned folder reported all seven as unexpected. Inherited objects are platform
config the child folder does not own — they are counted and reported as context,
never as drift. Found by running against the live tenant, not by reasoning.

### Known limit
`UNEXPECTED` cannot distinguish an orphan ("we made it, the intent was deleted")
from an unmanaged object ("someone made it by hand"). The tag-based engine can,
because a rule carries its own provenance. Here there is nothing to read, so both
collapse into one class and the report does not claim to know the cause.

**Tests: 408 → 421.**

## [1.2.0] — 2026-08-02

**`ZoneRequest` reaches the firewall.** Kind #2 has existed since #18 but never
had a Terraform resource behind it — v1.1.0 made that failure loud instead of
silent; this closes it. Zones now carry a full security posture, not just a name
and an interface list.

### Zones reach the device
- `scm_zone` resource + `zones` variable + module wiring. The root and module
  object types are byte-identical and the compile-time contract check
  (ADR-0004) enforces that per-attribute, so HOLE 3 cannot recur here.
- Rules **order after the zones they reference**: a rule's `from`/`to` resolves
  through `scm_zone.this[...]` for zones this module manages, while baseline
  zones (`local`, `internet`, `proxy`) pass through as plain strings. The
  predicate reads `var.zones`, not the resource, so the branch stays decidable
  at plan time — and it is deliberately not a blanket `depends_on`, which
  previously caused a destroy-cascade on address objects.

### Zone security posture (the ADR-0003 lesson, applied to zones)
A zone is not just a name and a port list. `ZoneRequest` now accepts:

| Field | Why it matters |
|---|---|
| `protection_profile` | Absent = **no** flood, reconnaissance or packet-based-attack protection |
| `user_id` | Off = any rule matching `source_user` **silently never matches** |
| `log_forwarding` | Otherwise local logs only |
| `device_id`, `dos_profile`, `dos_log_forwarding`, `user_acl`, `device_acl` | Full provider parity |

`protection_profile` is a **zone**-protection profile — flood/recon, bound to a
zone. Not the same thing as a rule's `profile`, which is a security profile
*group* giving IPS/AV/URL inspection. A zone with neither has neither. New
catalog: `catalog/zone-protection.yaml`.

### Risk classification for zones
`fwgitops classify` covers zones, having previously dropped them on the floor
("policy stages: rules only"):

- `zone_without_protection` (**HIGH**) — the `allow_without_inspection` lesson
  for zones: the absence of a security control is a finding, not a default.
  A LOW gate now refuses to auto-apply an unprotected zone.
- `user_id_disabled_on_zone` (LOW note) — the rule model has supported
  `source_user` since v1.0, and the failure is silent: the rule is skipped and
  traffic falls through to whatever is next.

### Fixed
`_load_zone_request` built its collector **without catalogs**, so a ZoneRequest's
reference names were never validated at all. A typo'd profile now fails at PR
time, as ADR-0003 requires for rules.

### Known limits
Zones cannot be drift-tracked: `scm_zone` has no `tag` attribute, so the
`gitops:` provenance markers `drift.py` relies on cannot be attached. Only 14 of
the provider's resources are taggable — this is a general limit of the
tag-based model, not a zone quirk. Tracked in `TODOS.md`.

**Tests: 392 → 408.**

## [1.1.0] — 2026-08-02

Closes a class of bug where the compiler produced config that **silently never
reached the firewall** while every check stayed green. Four distinct instances
were found and fixed; three of them were invisible to the 327-test v1.0 suite by
construction, because every test asserted the compiler wrote the right JSON and
stopped exactly where the failure began.

### ⚠ Behaviour change
`fwgitops compile` now **rejects** (exit 2, nothing written) when it would emit
into a folder that has no Terraform root. This previously succeeded silently.
Pass `--allow-missing-root` if you genuinely mean a scratch or scaffold
directory.

### The silent-drop holes

| # | Hole | Terraform's signal |
|---|---|---|
| 1 | tfvars key with no matching `variable` | warning, **exit 0** |
| 2 | `variable` declared but never referenced | **no diagnostic at all** |
| 3 | object attribute the target type omits | **silently discarded** |

**Hole 1** shipped for a full release: `zones.auto.tfvars.json` was written on
every compile while `terraform/prod-edge` declared no `zones` variable and the
module had no `scm_zone` resource. Compile, plan, apply and CI all green; the
zone never reached the device.

**Hole 3** was live in v1.0. The root module's `security_rules` type omitted the
six ADR-0003 attributes the module declares and the compiler emits —
`application`, `profile_group`, `log_setting`, `rulebase`, `relative_position`,
`target_rule` — so the module received its own defaults instead of the intent's
App-ID and profile. Root and module types are now identical.

### New — `fwgitops.tfcontract`
Checks holes 1 and 2 in pure Python, with no Terraform binary and no cloud
credentials, at compile time (fail-closed) and in CI. Hole 3 needs a
schema-level check; tracked in `TODOS.md`.

The parser is string-literal aware, which is load-bearing: a `}` inside a string
used to collapse brace depth and let an unwired variable pass, and a `//` inside
a URL was read as a comment and falsely rejected a valid module. Line breaks are
never masked — dropping one desynced the comment pass and truncated
`module "n" {` to `mo`.

### Fixed — CI guards that were not guarding
- `pr-validate` piped `terraform plan` through `tee` and appended `|| true`, so
  **every** plan failure was swallowed and `-detailed-exitcode` was meaningless.
  Exit 2 (changes present) is correctly treated as normal for a PR.
- `apply.yml` had no undeclared-variable check at all — the backstop guarded the
  preview but not the path that touches the device.
- Both workflows now fail when a folder has emitted tfvars but no Terraform root,
  instead of `continue`-ing past it.
- Plan artifacts upload with `if: always()`, so they survive the failure that
  makes them worth reading.

### Fixed — zone handling
- `catalog/environments.yaml` gains an optional `baseline_zones` list. It
  declared two baseline zones while the folder carries seven, so a rule
  referencing a real zone such as `proxy` was **rejected at compile time as
  undeclared** — fail-closed machinery producing a false negative.
- A `ZoneRequest` naming a zone that already exists on the device is rejected.
  The consistency check unions baseline and declared zones, so such a request
  looked maximally valid while Terraform would have created over a live zone.
- A valueless `baseline_zones:` key no longer errors — commenting out the list
  under its comment block is the natural edit and it used to brick compile.

### Security
`ScmCredentials.client_secret` is `repr=False`; the dataclass `__repr__`
rendered the live tenant secret in cleartext.

### Verified live (provider v1.0.11)
`scm_zone` writes its fields **faithfully** — the computed-attribute drop that
breaks `scm_security_rule` (ADR-0003) does not apply, so zones need no `enrich`
workaround. SCM also reference-validates zone fields fail-closed. Probe
committed at `spike/zone-probe/`; fidelity varies per resource type, so re-run it
before scoping `InterfaceRequest`.

**Tests: 327 → 382.** See [ADR-0004](docs/adr/0004-compiler-terraform-contract.md).

## [1.0.0] — 2026-07-29

First production release. **Day-2 security-rule provisioning is complete and
proven end-to-end on live hardware** (VM-Series, PAN-OS 11.2.12): a rule intent
flows `intent → compile → classify → risk-gate → terraform apply → enrich → push`
and lands in the firewall's running config, verified on-device via SSH.

### Rule model — full standard-policy expressiveness
A compiled rule now carries the complete set of common security-rule fields:
- **Match:** zones, source/destination addresses, **service**, **App-ID**
  (`application`), **User-ID** (`source_user`), **URL category** (`category`),
  and **negation** (`negate_source` / `negate_destination`).
- **Action:** `allow` / `deny` / `drop` / `reset-client` / `reset-server` /
  `reset-both`.
- **Inspection & logging:** security **profile group** (`profile`), external
  **log-forwarding** (`log_forwarding`), session **log_start** / **log_end**.
- **Placement:** rulebase + ordering (`top` / `bottom` / `before:<rule>` /
  `after:<rule>`).
- **Metadata:** `description`, provenance tags.

### The `enrich` step (provider-gap workaround)
The `paloaltonetworks/scm` Terraform provider silently drops `application`,
`profile_setting`, `log_setting`, ordering, and other rule fields (v1.0.11 and
v1.0.12-beta.3; see `docs/scm-provider-securityrule-bug.md`). `fwgitops enrich`
sets them via the SCM REST API after `terraform apply` and before `push`, so the
push commits skeleton + enrichment as one atomic change. Terraform owns the rule
skeleton + state/drift/rollback; enrich owns the fields the provider can't write.

### Safety & governance
- **Fail-closed everywhere:** invalid intent, unresolvable object, or unclassifiable
  change never produces a partial result.
- **Risk classifier** with a fail-closed tier gate (LOW auto / HIGH / CRITICAL);
  checks include broad match, any-any, internet exposure, novel zone-pair,
  shadowing, `allow_without_inspection`, and `negated_match`.
- **Name catalogs** validate App-ID / profile / log-forwarding names at PR time.
- **NIST-mapped evidence bundle** per change (records the full effective rule).
- **CI:** `pr-validate` runs tests + compile + classify + enrich preview + plan;
  `apply` auto-triggers on merge (fail-closed at LOW).

### Deferred to a later release
Day-1 provisioning (interfaces / IP / zones / virtual router), `schedule`, HIP
(`source_hip` / `destination_hip`), `policy_type: Internet`, and
`tenant_restrictions`.
