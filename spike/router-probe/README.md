# `scm_logical_router` fidelity probe — RUN, PASSED (2026-08-02)

Answers the P1 gate `RouteRequest` shipped with open: does the scm provider
actually WRITE nested static routes, or accept and silently drop them the way it
drops `profile_setting` / `log_setting` / ordering on `scm_security_rule`
(ADR-0003)?

## Result: faithful, routes included

Applied against the live tenant in `GitOps`, read back over the SCM API, then
destroyed. All seven checked paths honored:

| path | wanted | stored |
|---|---|---|
| `vrf.interface` | `["$eth-fwgitops-probe"]` | same |
| `route[probe-via-ip].destination` | `192.0.2.0/24` | same |
| `route[probe-via-ip].nexthop.ip_address` | `10.99.99.2` | same |
| `route[probe-via-ip].metric` | `17` | same |
| `route[probe-via-ip].admin_dist` | `33` | same |
| `route[probe-via-interface].destination` | `198.51.100.0/24` | same |
| `route[probe-via-interface].interface` | `$eth-fwgitops-probe` | same |

A second `terraform plan` reported **no changes** — no phantom diff, which is how
the `scm_security_rule` problem first showed itself (a `log_setting -> null`
clobber that never converged).

**So `RouteRequest` is a compiler + tfvars mapping. No `enrich` subsystem
needed**, unlike security rules.

## The record so far — fidelity is per resource type

| resource | writes faithfully? |
|---|---|
| `scm_security_rule` | **NO** — drops `profile_setting`, `log_setting`, ordering (ADR-0003) |
| `scm_zone` | yes (`spike/zone-probe`) |
| `scm_ethernet_interface` | yes (`spike/interface-probe`) |
| `scm_logical_router` | yes, four levels deep (here) |

Four for four at catching what inference would have got wrong or merely guessed.
Probe before building the next kind; do not extrapolate from this table.

## Blast radius

Everything created is NEW and lives in `GitOps`, which has **zero devices** and
which nothing inherits from:

* new router name and new interface name — overrides nothing, shadows nothing
* the probe interface is created here, so the router claims an interface no real
  VRF has ever held (an interface belongs to one VRF at a time)
* routes point at TEST-NET-1/2 (RFC 5737) via a /30 that exists nowhere — and
  never a default route

`var.folder` has a `validation` block refusing `prod-edge`, `ngfw-shared` and
`All`. Do not remove it.

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a
terraform -chdir=spike/router-probe init
terraform -chdir=spike/router-probe plan -out=probe.tfplan   # read-only, inspect first
terraform -chdir=spike/router-probe apply probe.tfplan
python3 spike/router-probe/readback.py --folder GitOps       # the only real evidence
terraform -chdir=spike/router-probe destroy -auto-approve
```

`readback.py` exits `0` faithful, `3` dropped or partial, `1` not found. It
distinguishes "no routes at all" from "routes landed but a field did not",
because those have different remedies.
