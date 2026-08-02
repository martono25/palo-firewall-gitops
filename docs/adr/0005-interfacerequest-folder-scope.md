# ADR-0005 — `InterfaceRequest` targets folder scope via the `$eth-*` variables

- **Status:** Accepted (direction; not yet built)
- **Date:** 2026-08-02
- **Deciders:** Martono, Claude

## Context

ADR-0002 sketched `InterfaceRequest` as `ethernet1/1 layer3, DHCP/static IP` — a
folder-local interface carrying its own addressing — and made it the first link
in the Day-1 chain. Read-only discovery against the live tenant showed the
mechanism it assumed does not match reality, so the design needed settling before
any code (or any probe) was worth writing.

### What the tenant actually has

```
All ──▶ ngfw-shared ──┬──▶ prod-edge ──┬── device 007955000893662  (PA-VM)
                      │                └── device 007955000894453  (PA-VM)
                      └──▶ GitOps
```

Interfaces are addressed two ways, and they are **the same object**:

| Scope | Name | Object id |
|---|---|---|
| `folder=ngfw-shared` | `$eth-internet` (`default_value: ethernet1/3`) | `7ff5e3ec-…` |
| `device=007955000893662` | `ethernet1/3` | `7ff5e3ec-…` |
| `folder=ngfw-shared` | `$eth-local` (`default_value: ethernet1/4`) | `35479f59-…` |
| `device=007955000893662` | `ethernet1/4` | `35479f59-…` |

Identical ids. The API renders the variable name at folder scope and the resolved
physical name at device scope. So `$eth-*` is not a separate object pointing at an
interface — it *is* the interface, under the name the folder knows it by.

Three further facts that shaped the decision:

- **`layer3` is `{}` at BOTH scopes, on both devices.** No addressing is
  configured anywhere. The interfaces exist (created by the NGFW baseline), and
  what is missing is precisely what ADR-0002 wanted declared.
- **No SCM variable binds `$eth-*`.** `/config/setup/v1/variables` holds only
  `$syslog1` in `prod-edge`. The interfaces resolve through `default_value`, not
  through a variable override.
- **Device serials appear in the folder listing but are NOT config folders.**
  `folder=007955000893662` returns *"Folder … doesn't exist"*; the correct scope
  parameter is `device=`. (Per the standing rule: check the request shape against
  <https://pan.dev/scm/docs/home/> before concluding anything about permissions.)

## Decision

**`InterfaceRequest` writes at FOLDER scope, addressing interfaces by their
`$eth-*` variable names.** It CONFIGURES an existing interface — sets `layer3`
addressing — rather than creating one.

ADR-0002's *intent* was right; only its mechanism was wrong. Correct that ADR's
chain to read "configure interface addressing", not "create an interface".

### Why not device scope

Addressing every device by its physical interface name would mean one intent file
per firewall, growing with the fleet, and would abandon an indirection the tenant
deliberately built. The `$eth-*` names exist so that one folder-level definition
serves devices whose physical layout may differ — which is exactly the
abstraction a GitOps pipeline wants.

### The cost, stated plainly

`ngfw-shared` is the parent of **both** `prod-edge` and `GitOps`. A folder-scope
interface change therefore reaches production and the sandbox together — a larger
blast radius than any change this pipeline has made to date.

This is accepted **on condition** that it is handled by controls that already
exist rather than by weakening the abstraction:

1. The risk classifier must treat any change scoped to a folder with child
   folders as **HIGH** at minimum, so the tier gate refuses to auto-apply it.
2. An interface change carries a `novel_addressing` style check — assigning an
   IP where `layer3` was previously `{}` is a materially different act from
   editing an existing address.
3. The same fail-closed contract checking that covers rules and zones applies
   (ADR-0004), including the per-attribute check, since
   `scm_ethernet_interface.layer3` is a nested object and HOLE 3 lives exactly
   there.

Writing to `ngfw-shared` was also why the `scm_ethernet_interface` fidelity probe
was **deliberately not run** — unlike zones there is no clean scratch target, and
fidelity of a resource whose design was unsettled is not the blocker. Run it
against a folder-scope interface once this design is being built.

## Consequences

**Positive**
- Matches how the tenant already models interfaces; no new vocabulary.
- One definition serves every device in a folder, which is the point of the
  indirection.
- ADR-0002's Day-1 chain becomes buildable against something real.

**Negative / cost**
- Largest blast radius of any kind so far, mitigated by classifier + gate rather
  than by design.
- `layer3` is a deeply nested provider type (`ip`, `dhcp_client`, `pppoe`,
  `arp`, …, and "exactly one of `dhcp_client` / `ip` / `pppoe`"). The intent
  schema must express that constraint, or the device commit will.
- **Interfaces cannot be drift-tracked.** `scm_ethernet_interface` has no `tag`
  attribute, like `scm_zone`. Only 14 of the provider's resources are taggable,
  so `drift.py`'s tag-based model would not cover this kind either. That is now
  two kinds out of three, and is the strongest argument for a second,
  state-based drift mechanism (`TODOS.md`).

## Related
- ADR-0002 — the Day-1 chain whose interface mechanism this corrects.
- ADR-0001 — the multi-kind model; `InterfaceRequest` would be kind #3.
- ADR-0004 — the fail-closed contract, including the per-attribute check that
  `layer3` will need.
