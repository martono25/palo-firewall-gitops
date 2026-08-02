# ADR-0002 — Day-1 provisioning: thin bootstrap + ordered config jobs

- **Status:** Accepted — **bootstrap half built**, config-job half not
- **Date:** 2026-07-27 (status revised 2026-07-31)
- **Deciders:** Martono, Claude

## Context

Initial provisioning must stand up a **complete, traffic-ready firewall** —
interfaces, IP addresses, zones, a virtual router, and a baseline policy — not
just onboard a bare device. Two ways to get there:

- **A — one monolithic `bootstrap.xml`**: a full PAN-OS config baked into the
  bootstrap package; the device is fully configured at first boot.
- **B — a thin bootstrap + the rest applied as declared config jobs** through the
  GitOps pipeline (the multi-kind model, ADR-0001).

## Decision

**Choose B.** Split by plane:

- **Bootstrap does the control-plane minimum — just enough to make the device
  *manageable*:** mgmt connectivity (DHCP), SCM onboarding (`panorama-server=cloud`,
  `dgname`, registration PIN), and licensing (auth code). (This is ~what
  `provisioning/aws-vmseries-pilot` already does.)
- **The pipeline does the data-plane** — interfaces, IPs, zones, virtual router,
  NAT, baseline policy — as **declared jobs applied in dependency order** (per
  ADR-0001):

  ```
  bootstrap → device connected + licensed + in folder
     └─ InterfaceRequest  (ethernet1/1 layer3, DHCP/static IP)
          └─ ZoneRequest   (bind interfaces → zones)
               └─ RouteRequest / VR
                    └─ AccessRequest (baseline + specific rules)
  ```

  A Day-1 "provision a complete firewall" run is therefore a **curated, ordered
  set of the same multi-kind jobs used for Day-2** — one mechanism, not two.

### Why not the monolithic `bootstrap.xml` (A)

- **Clobber risk** — a full config fights the EC2 SSH-key injection, system
  users, and SCM's own pushes (same reasoning that moved the admin password to
  post-boot SSH, ADR-in-spirit).
- **Ungoverned** — an XML blob skips classify/gate/evidence/drift; a new
  internet-facing interface is a high-risk change that should be *reviewed*.
- **Split-brain** — "bootstrap set X, SCM manages Y" is a reconciliation and
  drift-detection nightmare.
- **Rigid** — interface IPs are per-firewall, forcing bespoke `bootstrap.xml`
  generation per device; the pipeline handles per-device values as variables.

## Consequences

**Positive**
- One mechanism spans Day-1 buildout and Day-2 changes.
- The full device config (network included) is declared in Git and governed by
  the whole loop.
- Bootstrap stays intentionally dumb, minimal, and stable — no clobber risk.

**Negative / cost**
- Requires **cross-kind dependency ordering** (ADR-0001) — the real engineering.
- A brief **connected-but-not-traffic-ready** window during Day-1 buildout while
  the config jobs apply. Acceptable, arguably desirable — each step is reviewed
  and gated rather than shipped in one opaque boot artifact.

## Implementation status (2026-07-31)

**Built — the control-plane half.** `provisioning/aws-vmseries-pilot` (VPC,
bootstrap bucket, IAM, init-cfg), plus `provision.py`, `onboard.py` and
`admin_password.py`. A VM-Series has been booted, onboarded and proven on live
hardware.

**Not built — most of the data-plane half.** None of `InterfaceRequest`,
`RouteRequest` or `NatRequest` exists. Cross-kind dependency ordering — named
above as "the real engineering" — is not built either.

**`ZoneRequest` is now complete** (v1.2.0): it compiles to `scm_zone`, reaches
the device, carries the full security posture (zone-protection profile,
User-ID/device-ID, log forwarding, DoS, ACLs), is catalog-validated at PR time
and risk-classified. Rules order after the zones they reference. It had existed
since #18 but never reached the firewall (ADR-0004).

**Consequence worth stating plainly.** The ordered chain above is
`InterfaceRequest → ZoneRequest → RouteRequest → AccessRequest`. Zones bind
interfaces, so with no `InterfaceRequest` the only zone that survives a device
commit is one with an **empty interface list** — a zone that carries no traffic.
Finishing `ZoneRequest` alone therefore does not produce a usable Day-1 build;
`InterfaceRequest` is the real prerequisite. This was missed once already when
scoping v2.0 around zones.

## The tenant contradicts this ADR's interface design (2026-08-02)

Read-only discovery against the live SCM tenant, before writing any code for
`InterfaceRequest`. **This ADR's interface model does not match reality**, so do
not build to the chain above without reworking this first.

This ADR describes `InterfaceRequest` as `ethernet1/1 layer3, DHCP/static IP` —
a folder-local interface carrying its own addressing. What the tenant has:

```
All ──▶ ngfw-shared ──┬──▶ prod-edge
                      └──▶ GitOps

$eth-internet   default_value = ethernet1/3   defined in ngfw-shared   layer3 = {}
$eth-local      default_value = ethernet1/4   defined in ngfw-shared   layer3 = {}
```

Three mismatches, each one load-bearing:

1. **Interfaces are named variables, not literal names.** Zones reference
   `$eth-local` / `$eth-internet`, not `ethernet1/1`. Each is an object with a
   per-device `default_value`, which is what lets one folder config serve
   devices whose physical interfaces differ.
2. **They live in the SHARED PARENT folder and are inherited.** They are defined
   once in `ngfw-shared` and appear in both `prod-edge` and `GitOps`. An
   `InterfaceRequest` therefore either writes to a folder that feeds production
   *and* the sandbox, or creates a local override of an inherited object. Both
   are materially different from the folder-local create this ADR assumes, and
   the first has far more blast radius than any Day-2 change to date.
3. **SCM stores no addressing for them** — `layer3` is `{}` on both. Whatever
   assigns IPs is not in the SCM folder config, so "declare the interface's IP
   through the pipeline" has no target here as written.

Consequences for the wider design:

- **ADR-0001's device-inventory idea validates the wrong vocabulary.** Checking
  a zone's interface names against a device's *physical* interfaces is the wrong
  predicate; the real vocabulary is the inherited `$eth-*` variable set, and any
  catalog has to understand folder inheritance to resolve it.
- **Four of the seven live zones carry no interfaces at all** (`layer3: []`), and
  `proxy` has no `network` block. The "zone that carries no traffic" state is not
  hypothetical — it is most of the tenant.
- **`scm_ethernet_interface` has no `tag` attribute**, like `scm_zone`. Only 14
  of the provider's resources are taggable, so `drift.py`'s tag-based model
  structurally covers a minority of object types. That is a general limit, not a
  zone-specific quirk.

**Do this before scoping `InterfaceRequest`:** decide what it manages (the
shared-folder variable, a local override, or nothing because bootstrap owns it),
and only then probe that resource's provider fidelity. Probing
`scm_ethernet_interface` first was deliberately skipped — fidelity of a resource
the design may not use is not the blocker.

## Related
- ADR-0001 (multi-kind intent model) — the enabling mechanism.
- ADR-0004 — the silent-dead-end bug and the fail-closed contract that now
  prevents a kind from being half-wired again.
- Current bootstrap: `provisioning/aws-vmseries-pilot` (init-cfg + license + IAM).
- Admin password is set post-boot over SSH (route B), not in the bootstrap — same
  clobber-avoidance rationale.
