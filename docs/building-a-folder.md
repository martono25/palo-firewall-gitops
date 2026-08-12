# Building a folder: the Day-1 chain, end to end

**Audience: the platform team.** If you want a firewall rule, you want
[`requesting-rules.md`](requesting-rules.md) instead — this is about standing up
the scope those rules land in.

> **This is a reconstruction of a real build, not an imagined one.** Every step
> below is how `prod-edge` and the pilot firewall `007955000902404` were actually
> brought up, and every file it names is in this repository. Where something
> failed the first time, it says so — those are the parts you would otherwise
> rediscover against a production firewall.
>
> Each `spec:` shown is **verbatim** from the file it links, and a test asserts
> that it stays that way. The `metadata:` blocks are abbreviated for reading —
> the real `justification` fields are longer. So trust the behaviour shown here
> exactly, and read the linked file for the full record.

---

## The order is not a convention

```
InterfaceRequest → ZoneRequest → RouteRequest → AccessRequest
```

Ask the tool rather than trusting this page:

```sh
fwgitops kinds --order
```

The order is declared per kind in the registry (`depends_on_kinds`) and the apply
pipeline consumes it, so it cannot drift from what actually runs. It exists
because the chain **spans Terraform roots**: interfaces are device-scoped and
live in `terraform/device-<serial>/`, while zones, routes and rules are
folder-scoped and live in `terraform/<folder>/`. Two roots, two states, no single
graph — so the ordering has to be explicit.

Each link needs the one before it:

* a **zone** binds an interface, so the interface object must exist
* a **route** is only reachable once its VRF's interfaces are addressed
* a **rule** references zones

---

## Before you start: does the repo know your firewall?

A Day-1 intent names its firewall **by serial**, so a freshly provisioned or
rebuilt device needs the repository pointed at it first. Check:

```sh
grep -rn 'device:' intent/ | grep -v '#'
fwgitops verify-catalog          # catalog vs SCM's real hierarchy
```

If those serials are not the firewall you have, the one-command fix is:

```sh
fwgitops adopt-device <serial> --folder <folder> --replacing <old-serial>
```

For the full sequence around it — the Terraform root, the old state, the serial
in `tests/` — stop and do
[`operator-runbook.md` § Replacing a firewall, steps 4-8](operator-runbook.md#replacing-a-firewall-new-serial)
first. `compile` will reject an intent naming a firewall the catalog does not
declare, so you cannot get far with the serial alone wrong — but the interface
PORT map is not checked against SCM, and that one is silent.

## Part 0 — before any intent will compile

This is where a greenfield folder actually gets stuck, and none of it lives in
the intent files.

### 1. The folder exists in SCM, and in the catalog

`catalog/folders.yaml` is a hand-maintained mirror of SCM's hierarchy:

```yaml
folders:
  prod-edge:
    children: []
    targetable: true
    devices:
      "007955000902404":
        display_name: fw-prod-edge-4453
```

`targetable: false` means intents may not name it — a parent folder like
`ngfw-shared` is never a target, because a change there reaches everything
beneath it.

Verify the mirror against reality before trusting it:

```sh
fwgitops verify-catalog
```

This has caught two real drifts: device serials listed as child *folders*, and a
firewall that left SCM while the catalog still called it targetable. Both produce
the same failure — an intent that compiles clean and dies at apply.

### 2. Every scope has a Terraform root

One root per scope, one state per root. Generate them from the module rather than
hand-writing:

```sh
fwgitops scaffold-root --folder prod-edge
fwgitops scaffold-root --device 007955000902404 --device-folder prod-edge
fwgitops scaffold-root --check          # CI runs this
```

A root must mirror the module **attribute for attribute**. Terraform silently
discards an undeclared object attribute at the module boundary — no warning, exit
0 — so a drifted root does not fail, it quietly stops delivering part of every
intent (ADR-0004, HOLE 3). `--sync` regenerates after a module change.

### 3. Folder interface variables exist

**Run this before compiling anything**, and note why it cannot be driven off the
intent tree:

```sh
fwgitops folder-interfaces
```

On this tenant a folder-scope interface is a `$`-prefixed **variable** whose
`default_value` names the physical port each firewall resolves it to. A zone can
only bind an interface object that exists **at its scope** — binding a literal
port name is refused as an invalid reference — so these variables are what make a
folder's zones bindable at all.

A greenfield folder has no intents yet, so deriving them from the intent tree
would be circular. The source is `catalog/interfaces.yaml`, which is
platform-maintained and changed by PR, so a requester still cannot conjure a
physical port.

```yaml
interfaces:
  dmz:
    folder: $eth-dmz
    create_in:
      prod-edge: ethernet1/2      # this platform CREATES this variable
    devices:
      "007955000902404": ethernet1/2
```

`local` and `internet` have **no `create_in`** — they are SCM defaults inherited
from `ngfw-shared`, not ours to create. `dmz` is the first role this platform
creates itself.

---

## Part 1 — InterfaceRequest: address the ports

Reference file: [`intent/prod/edge-example/REQ-2026-0418.example.yaml`](../intent/prod/edge-example/REQ-2026-0418.example.yaml)

> These three parts cite `*.example.yaml` files rather than the intents
> currently deployed. That is deliberate: the guide used to be pinned to the
> live tree, which meant **deleting your last zone broke the build** — the
> deletion contract in [ADR-0008](adr/0008-deletion-contract.md) could not be
> exercised on the last instance of a kind. The examples are permanent and are
> loaded by the real validator in the test suite, so they cannot drift from the
> schema; what they no longer promise is that a firewall is running exactly
> this. For what is deployed, read `intent/` directly.

```yaml
apiVersion: fw-intent/v1
kind: InterfaceRequest
metadata:
  id: REQ-2026-0418
  requester: jane.doe@corp
  ticket: JIRA-12346
  justification: "Day-1 build: address the internal interface"
  requested: 2026-08-04
spec:
  device: "007955000902404"     # a SERIAL, not a folder
  interface: local              # a ROLE from catalog/interfaces.yaml
  ip:
    - 10.100.3.125/24
  mtu: 1500
  comment: "Internal — managed by fwgitops"
```

**`device:`, never `folder:`.** Addressing is per-firewall — two firewalls cannot
share an IP. A firewall is the last level of the SCM hierarchy and inherits down
it, but it is *addressed* `device=<serial>`; `folder=<serial>` returns
400 "Folder doesn't exist".

**`interface:` is a role, not a port.** The catalog maps `local` → `ethernet1/4`
on this serial. A firewall absent from that mapping cannot be targeted for the
role — fail closed rather than guess a port.

**It configures an interface that already exists** (ADR-0005); it cannot create
one. On this tenant the interfaces exist with `layer3` empty, and what an
`InterfaceRequest` supplies is the addressing.

> **On removal:** the device-scope override reverts to the **inherited** object,
> which carries no addressing. The firewall loses the IP on that interface.

The pilot took three: `local`, `internet` (REQ-2026-0802) and `dmz`
(REQ-2026-0805).

---

## Part 2 — ZoneRequest: declare the zone, bind the interface

Reference file: [`intent/prod/edge-example/REQ-2026-0419.example.yaml`](../intent/prod/edge-example/REQ-2026-0419.example.yaml)

```yaml
apiVersion: fw-intent/v1
kind: ZoneRequest
metadata:
  id: REQ-2026-0419
  requester: jane.doe@corp
  ticket: JIRA-12347
  justification: "Declare the dmz zone and bind it to the DMZ interface"
  requested: 2026-08-05
spec:
  folder: prod-edge
  zone: dmz
  type: layer3
  interfaces:
    - $eth-dmz                  # the FOLDER-scope variable
  protection_profile: best-practice
  log_forwarding: log-best
```

**`folder:` here, not `device:`.** A zone is policy structure, shared by every
firewall in the folder.

**`interfaces:` takes the folder-scope variable** (`$eth-dmz`), which is what
`fwgitops folder-interfaces` created in Part 0. This is the join between the two
halves of the chain.

**A zone with no `protection_profile` has no flood or reconnaissance
protection.** The classifier reports that, and reports `user_id_disabled_on_zone`
when User-ID is off. Both are facts about the zone, not blockers — HIGH is
approvable.

> **On removal:** SCM **refuses** the delete while any rule references it
> (409 `NON_ZERO_REFS`, naming the referencing path). Unreferenced, it deletes
> cleanly and the interface survives **addressed but unzoned** — and PAN-OS drops
> traffic on an unzoned interface. Tested end to end 2026-08-05.

---

## Part 3 — RouteRequest: the most dangerous link

Reference file: [`intent/prod/edge-example/REQ-2026-0420.example.yaml`](../intent/prod/edge-example/REQ-2026-0420.example.yaml)

```yaml
apiVersion: fw-intent/v1
kind: RouteRequest
metadata:
  id: REQ-2026-0420
  requester: jane.doe@corp
  ticket: JIRA-12348
  justification: "Default route out the untrust interface"
  requested: 2026-08-04
spec:
  folder: prod-edge
  destination: 0.0.0.0/0
  nexthop: 10.100.2.1
  metric: 10
```

**Routes aggregate.** Many `RouteRequest`s become one `scm_logical_router`,
because a route lives four levels inside a router object that also carries the
VRF's interface membership. The router and VRF come from
`catalog/routers.yaml`; the request supplies the route.

A default route classifies **HIGH** (`default_route`) — it decides where all
unmatched traffic goes — so it will not auto-apply. Clearing it takes a
deliberate `workflow_dispatch` at a higher tier.

> **On removal: nothing refuses it, at any layer.** Measured 2026-08-06: the
> `prod-edge` override was destroyed, the folder reverted to the inherited
> `ngfw-shared` router, and the default route disappeared from the device about
> **40 seconds after the push reported success**. VRF interface membership and
> connected routes survived, intra-subnet traffic kept working, and everything
> off-subnet was **black-holed**. No error, no rollback.

That 40-second lag is the general lesson: **a successful push does not mean the
device has the change.** Anything asserting "it is live" must poll the device.

---

## Part 4 — the folder is ready for rules

With interfaces addressed, a zone declared and a route in place, the folder can
take `AccessRequest`s.

### → NEXT: prove it end to end, then hand it over

**1. Confirm the chain reached the device.** SCM holding it is not the same as
the firewall running it:

```sh
fwgitops device-sync
printf 'set cli pager off\nshow interface all\n' | ssh -T -i <key>.pem admin@<mgmt-ip>
```

Interfaces should be up with addresses and zones — not placeholder MACs.

**2. Request one rule yourself**, as a requester would:
[`requesting-rules.md`](requesting-rules.md). The fastest version is the Issue
Form — open an issue, fill it in, and the platform opens the pull request. That
is the path app teams use, and walking it once is how you find out whether it
works for someone who is not you.

**3. Then you are running it.** Day to day is
[`operator-runbook.md`](operator-runbook.md): a run waiting for approval, drift
firing, a rule to remove, the token expiring.

---

## Applying the chain

**You do not run the apply. Merging to `main` runs it.** `apply.yml` triggers on a
push to `main` touching `intent/**`, `catalog/**` or `terraform/**`, so the whole
sequence is an ordinary pull request.

### 1. Check locally first

```sh
fwgitops compile intent --check     # fail-closed validation, writes nothing
fwgitops classify intent            # per-change risk tier
```

**If you EDITED an existing intent rather than adding one, give it a new
ticket.** A changed `spec` with the old `metadata.ticket` is rejected — otherwise
the evidence bundle for this change cites the request that authorised the
previous one:

```
REQ-2026-0801: `spec` changed but `metadata.ticket` is still 'JIRA-902'
```

Update `ticket`, `requested`, and `justification` if the reason differs.

### 2. Open a pull request

**Starting on `main` with uncommitted changes:**

```sh
git checkout -b day1/<what-this-does>
git add intent/ catalog/ terraform/          # by name, not -A
git commit                                   # if anything is REMOVED, the PR body
                                             # needs `Removes: <REQ-id> (TICKET)`
git push -u origin HEAD
gh pr create --fill
gh pr checks --watch
```

**Already on a branch** — which you are if you came from
[`operator-runbook.md` § Replacing a firewall](operator-runbook.md#replacing-a-firewall-new-serial),
since the catalog and Terraform edits are committed there. Do not branch again;
you would fork the branch you are on. Add only what is still uncommitted:

```sh
git status --short                           # what is actually outstanding
git add intent/                              # or whatever `status` lists
git commit
git push
gh pr create --fill
gh pr checks --watch
```

Check `git branch --show-current` if you are unsure which case you are in.

`pytest` and `compile-and-plan` must pass — they are required, and `main` takes
no direct push.

### 3. Merge, and watch the apply it starts

```sh
gh pr merge --squash --delete-branch
gh run list --workflow apply.yml --limit 1
gh run watch <run-id>
```

The run classifies first, then applies each Terraform root in dependency order.

### 4. Approve it if the tier says so

A LOW changeset applies unattended. HIGH or CRITICAL holds at the
`firewall-apply` environment for a named reviewer, and the run sits at `waiting`
until someone approves it — see
[`operator-runbook.md` § A run is waiting for you](operator-runbook.md#a-run-is-waiting-for-you).

A Day-1 chain containing a default route is HIGH, so **expect to approve it**.

### 5. Merge the evidence pull request

The apply opens `evidence: bundles for <sha>`. Merge it; the record is not in the
source of truth until you do.

The apply pipeline walks scopes in dependency order — `fwgitops apply-order`
drives the loop, so the device root applies before the folder root. It fails
(exit 2) when kinds are interleaved across roots such that no whole-root order
works, rather than picking one and hoping.

**The tier picks the approver.** `classify --max-tier` grades the CHANGESET — not
the tree — and the workflow routes on the answer: LOW goes to
`firewall-apply-auto` and applies unattended, HIGH and CRITICAL go to
`firewall-apply` and wait for its required reviewer.

Grading the tree instead is not a theoretical difference. The pilot declares a
default route, which is permanently HIGH, so a whole-tree maximum meant every
apply routed to a human and LOW auto-apply was unreachable on any repo that had
ever declared one. The routing shipped inert that way and a live run found it,
not the tests.

There is no input to raise or lower the tier. One existed and was removed: it
asked a human to restate what the classifier had already computed, which is two
sources of truth for one fact.

Afterwards, each change has a record:

```
evidence/device-007955000902404/REQ-2026-0801.json
evidence/prod-edge/REQ-2026-0806.json
```

Scope-keyed, so a device-scoped change lands in `device-<serial>/`, mirroring the
Terraform roots.

**The bundles arrive as their own pull request**, titled `evidence: bundles for
<sha>`, opened by the apply run. Merge it — the apply already happened, and the
PR is what puts the record in the source of truth. `main` takes no direct push
from anyone, including the workflow, because a push to `main` is what triggers an
apply; a pipeline exempt from the rule it enforces is not enforcing it.

---

## Two things that went wrong the first time

**Three spikes concluded device scope was unsupported. All three were wrong.**
Zones, routers and rules refused a device-scope write with `"Device <serial>
doesn't exist"` while an ethernet interface succeeded on the same firewall in the
same run. The firewall was in a **broken registration state**, and the control
used in every probe — `scm_ethernet_interface` — happened to be the one resource
still working. After an offboard and re-onboard, every resource accepted a
device-scope write.

A positive control proves the path is alive; it does not prove the environment is
healthy, and it is worth least exactly when the passing case is the anomaly. When
an error names a precondition, **test the precondition** rather than arguing from
adjacent evidence.

**A re-onboard wipes device-scope config.** The 2026-08-05 re-onboard silently
removed all three interface overrides while the firewall kept running its old
config. The display name also reset to `PA-VM`, which is cosmetic — but it is a
reliable *symptom* of a re-registration, which is why `verify-catalog` compares
it.

---

## Where to look next

| | |
|---|---|
| Rule requests | [`requesting-rules.md`](requesting-rules.md) |
| Why the chain is ordered | [`adr/0002-day1-provisioning-thin-bootstrap.md`](adr/0002-day1-provisioning-thin-bootstrap.md) |
| Why Day-1 kinds name a folder | [`adr/0006-day1-kinds-target-a-folder.md`](adr/0006-day1-kinds-target-a-folder.md) |
| What removal means per kind | [`adr/0008-deletion-contract.md`](adr/0008-deletion-contract.md) |
| The compiler → Terraform contract | [`adr/0004-compiler-terraform-contract.md`](adr/0004-compiler-terraform-contract.md) |
