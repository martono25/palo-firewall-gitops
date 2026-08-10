# Building a folder: the Day-1 chain, end to end

**Audience: the platform team.** If you want a firewall rule, you want
[`requesting-rules.md`](requesting-rules.md) instead — this is about standing up
the scope those rules land in.

> **This is a reconstruction of a real build, not an imagined one.** Every step
> below is how `prod-edge` and the pilot firewall `007955000894453` were actually
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
      "007955000894453":
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
fwgitops scaffold-root --device 007955000894453 --device-folder prod-edge
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
      "007955000894453": ethernet1/2
```

`local` and `internet` have **no `create_in`** — they are SCM defaults inherited
from `ngfw-shared`, not ours to create. `dmz` is the first role this platform
creates itself.

---

## Part 1 — InterfaceRequest: address the ports

Real file: [`intent/prod/edge-fw-4453/REQ-2026-0801.yaml`](../intent/prod/edge-fw-4453/REQ-2026-0801.yaml)

```yaml
apiVersion: fw-intent/v1
kind: InterfaceRequest
metadata:
  id: REQ-2026-0801
  requester: martono@corp
  ticket: JIRA-901
  justification: "Day-1 build: address the internal interface"
  requested: 2026-08-04
spec:
  device: "007955000894453"     # a SERIAL, not a folder
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

Real file: [`intent/prod/edge-fw-4453/REQ-2026-0806.yaml`](../intent/prod/edge-fw-4453/REQ-2026-0806.yaml)

```yaml
apiVersion: fw-intent/v1
kind: ZoneRequest
metadata:
  id: REQ-2026-0806
  requester: martono@corp
  ticket: JIRA-906
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

Real file: [`intent/prod/edge-fw-4453/REQ-2026-0803.yaml`](../intent/prod/edge-fw-4453/REQ-2026-0803.yaml)

```yaml
apiVersion: fw-intent/v1
kind: RouteRequest
metadata:
  id: REQ-2026-0803
  requester: martono@corp
  ticket: JIRA-904
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
take `AccessRequest`s. That is [`requesting-rules.md`](requesting-rules.md), and
app teams write those.

---

## Applying the chain

Compile everything and check what each change is worth:

```sh
fwgitops compile intent --check     # fail-closed validation, writes nothing
fwgitops classify intent            # per-change risk tier
```

The apply pipeline walks scopes in dependency order — `fwgitops apply-order`
drives the loop, so the device root applies before the folder root. It fails
(exit 2) when kinds are interleaved across roots such that no whole-root order
works, rather than picking one and hoping.

A push to `main` applies at **LOW** only. The pilot's chain contains a HIGH
default route, so the whole apply stops at the risk gate until someone dispatches
it deliberately at a higher tier — and the `firewall-apply` environment then
waits for a reviewer.

Afterwards, each change has a record:

```
evidence/device-007955000894453/REQ-2026-0801.json
evidence/prod-edge/REQ-2026-0806.json
```

Scope-keyed, so a device-scoped change lands in `device-<serial>/`, mirroring the
Terraform roots.

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
