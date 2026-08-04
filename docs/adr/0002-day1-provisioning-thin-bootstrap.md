# ADR-0002 — Day-1 provisioning: thin bootstrap + ordered config jobs

- **Status:** Accepted — **BUILT**. Bootstrap, every config-job kind (proven on
  hardware), and the cross-kind ordering that sequences them. `NatRequest`
  remains deferred to v2.0; `ZoneRequest` has still never reached a device.
- **Date:** 2026-07-27 (status revised 2026-07-31, again 2026-08-04)
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

**Data-plane half — partially built.** `InterfaceRequest` (v1.8.0) and
`ZoneRequest` (v1.2.0) are done and reach the firewall. `RouteRequest` is the
remaining link in the ordered chain. **`NatRequest` is deferred to v2.0**
(decision 2026-08-02): it is not in the chain, so it does not block Day-1.
Cross-kind dependency ordering — named above as "the real engineering" — is not
built either.

> **Superseded 2026-08-04.** `RouteRequest` shipped and the whole chain now runs
> on hardware. See "Implementation status (2026-08-04)" at the end of this ADR;
> the paragraph above is kept for the record, not as current state.

Worth knowing before either is scoped: `scm_logical_router` has no `tag`
attribute (so `RouteRequest` would use state-based drift, already generic),
while `scm_nat_rule` DOES (so `NatRequest` would use the tag-based engine and
get orphan-vs-unmanaged attribution). Their drift stories differ, which is part
of why they are sequenced separately.

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

**RESOLVED by ADR-0005 (2026-08-02).** `InterfaceRequest` targets FOLDER scope
via the `$eth-*` names and CONFIGURES an existing interface (sets `layer3`)
rather than creating one — `$eth-local` and `ethernet1/4` turned out to be the
same object under two names, with `layer3` empty on both devices. The intent in
the chain above is right; read it as "configure interface addressing", not
"create an interface". The `scm_ethernet_interface` fidelity probe remains unrun
by design; run it against a folder-scope interface when this is built.

## Related
- ADR-0001 (multi-kind intent model) — the enabling mechanism.
- ADR-0004 — the silent-dead-end bug and the fail-closed contract that now
  prevents a kind from being half-wired again.
- Current bootstrap: `provisioning/aws-vmseries-pilot` (init-cfg + license + IAM).
- Admin password is set post-boot over SSH (route B), not in the bootstrap — same
  clobber-avoidance rationale.

## Implementation status (2026-08-04) — the chain runs; the ordering does not

Supersedes the 2026-07-31 status above. Every link is now built AND exercised
against the live firewall, verified in the RUNNING config rather than in SCM:

```
ethernet1/3   up   10.100.2.142/24   zone internet   lr:default   <- REQ-2026-0802
ethernet1/4   up   10.100.3.125/24   zone local      lr:default   <- REQ-2026-0801
0.0.0.0/0     static via 10.100.2.1  ACTIVE          ethernet1/3  <- REQ-2026-0803
5 AccessRequest rules in the pushed policy
```

and the firewall was observed evaluating real packets against a rule compiled
from intent — HTTP allowed, ICMP denied, both logged with rule name, ports and
end reason (`spike/`-era harness, since torn down).

### What is NOT done, in the order it matters

**1. The ordering is not mechanised — this is the substantive gap.** This ADR's
proposition is a *"curated, ordered set of the same multi-kind jobs"*, and that
does not exist. There is a cross-kind CHECK (a rule may only use a zone the
folder declares) but no sequencing. The chain above was ordered BY HAND, one PR
at a time. Nothing today stops a fresh run applying a route before the interface
it depends on, or a rule before its zone. "Cross-kind dependency ordering — the
real engineering" remains exactly that.

**2. `ZoneRequest` has never reached hardware.** It is built, unit-tested and
provider-probed, but on this tenant the zones already exist in `ngfw-shared` and
bind to the `$eth-*` variables, so they attached themselves the moment the
interfaces were addressed. No `ZoneRequest` was needed or written. On a firewall
in a fresh folder it would be, and it is the one link in the chain with no live
evidence behind it. Do not read the chain's success as evidence for this kind.

**3. `NatRequest` is still deferred to v2.0**, so a firewall that needs outbound
NAT cannot yet be built from intent alone.

### The honest summary

Every KIND works, proven on hardware. The WORKFLOW — provisioning a firewall as
one ordered operation — does not exist yet; a human sequences it. That is the
difference between "the parts are built" and "Day-1 provisioning is a thing you
can run", and it is the last item in this ADR that is neither done nor
deliberately deferred.

## Ordering built (2026-08-04, v1.17.0)

"Cross-kind dependency ordering — the real engineering" is now built, which
closes the last item in this ADR that was neither done nor deferred.

**Declared in the registry, not hard-coded.** Each `KindHandler` carries
`depends_on_kinds`, so a new kind states its own requirements and the sequencing
follows — the same reasoning that put `drift_engine` there. `kind_apply_order()`
topologically sorts them, tie-broken alphabetically so two runs of the same
registry produce the same sequence. A build that is not reproducible is not
ordered, it is merely lucky.

```
InterfaceRequest -> ZoneRequest -> RouteRequest -> AccessRequest
```

**It exists because the chain SPANS ROOTS.** Inside one root Terraform orders by
resource reference and does it better — `scm_security_rule` already references
`scm_zone.this[z].name`. But interfaces are DEVICE-scoped while zones, routes and
rules are FOLDER-scoped, so they live in separate states that no single graph
covers. That is the gap this fills, and it is the whole gap.

**`fwgitops apply-order`** prints Terraform roots in that order, and the apply
workflow consumes it. It previously ran `for dir in terraform/*/` — alphabetical.
On this tenant `device-<serial>` sorts before `prod-edge`, so interfaces happened
to apply before what depends on them: correct by accident, and it would have
silently inverted on a rename.

**Fails closed, three ways.** A dependency cycle, a dependency naming an
unregistered kind, and — the interesting one — kinds INTERLEAVED across roots
such that no whole-root order can satisfy them. That last case needs per-kind
applies and is a real design change, so it exits 2 with the conflicting pair
named rather than picking an arbitrary sequence that looks like success.

**Still true, and not addressed here:** `ZoneRequest` has never reached hardware
(this tenant's zones pre-exist and self-attach), and `NatRequest` is deferred.
Ordering does not change either.
