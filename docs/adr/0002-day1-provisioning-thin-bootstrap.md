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

**Not built — the data-plane half.** None of `InterfaceRequest`, `RouteRequest`
or `NatRequest` exists. `ZoneRequest` exists but does not reach the firewall
(ADR-0004). Cross-kind dependency ordering — named above as "the real
engineering" — is not built either.

**Consequence worth stating plainly.** The ordered chain above is
`InterfaceRequest → ZoneRequest → RouteRequest → AccessRequest`. Zones bind
interfaces, so with no `InterfaceRequest` the only zone that survives a device
commit is one with an **empty interface list** — a zone that carries no traffic.
Finishing `ZoneRequest` alone therefore does not produce a usable Day-1 build;
`InterfaceRequest` is the real prerequisite. This was missed once already when
scoping v2.0 around zones.

## Related
- ADR-0001 (multi-kind intent model) — the enabling mechanism.
- ADR-0004 — the silent-dead-end bug and the fail-closed contract that now
  prevents a kind from being half-wired again.
- Current bootstrap: `provisioning/aws-vmseries-pilot` (init-cfg + license + IAM).
- Admin password is set post-boot over SSH (route B), not in the bootstrap — same
  clobber-avoidance rationale.
