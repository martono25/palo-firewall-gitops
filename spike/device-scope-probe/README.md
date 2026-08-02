# `device=` scope probe — RUN, PASSED (2026-08-02)

Can a single firewall be targeted, and does the provider write faithfully there?

This exists because v1.11.0 assumed SCM creates a folder per device and shipped
that into a catalog, an ADR and a test. It does not: `folder=<serial>` returns
400 "Folder doesn't exist", and the serial is a `device=` scope (ADR-0006
correction note). Targeting one firewall needs device scope — so, per the rule
that has now caught something four times, probe before building.

## Result: device scope isolates, and the provider is faithful

Applied against the **disconnected** firewall, read back, destroyed.

**1. Fidelity** — all honored:

| field | wanted | stored |
|---|---|---|
| `comment` | `fwgitops device-scope probe …` | same |
| `layer3.mtu` | `1500` | same |
| `layer3.ip` | `[{name: 10.99.98.1/30}]` | same |

**2. Isolation** — the object stayed put:

| scope | probe visible? | |
|---|---|---|
| `device=007955000893662` (target) | present | expected |
| `device=007955000894453` (other firewall) | absent | expected |
| `folder=ngfw-shared` (shared parent) | absent | expected |

The stored object carried `folder=None`, `device=007955000893662`, and a **new
id** (`3a8e751d…`) distinct from the shared interface objects. A second
`terraform plan` reported no changes — no phantom diff.

Post-destroy the tenant was verified back to its exact prior state: both devices
showing only `ethernet1/3` / `ethernet1/4` with `layer3: {}`, and `ngfw-shared`
unchanged.

## What this does NOT answer

Device scope projects the **same object ids** as the shared folder-scope
interfaces:

```
folder=ngfw-shared   $eth-internet (default_value: ethernet1/3)   7ff5e3ec-…
device=<serial>      ethernet1/3                                  7ff5e3ec-…
```

This probe used a **new** name (`ethernet1/5`), so it never touched them. Whether
writing an **existing inherited** interface at device scope creates a per-device
override or mutates the object every device inherits is still open — and that is
the case `InterfaceRequest` actually needs, since ADR-0005 says it configures
interfaces that already exist.

Answering it means writing to an object `prod-edge` inherits. Do that
deliberately, on the disconnected device, with an immediate read-back of
`ngfw-shared` and the other firewall, and a destroy that verifies restoration.

## The record so far — fidelity and scope are per resource

| resource | writes faithfully? |
|---|---|
| `scm_security_rule` | **NO** — drops `profile_setting`, `log_setting`, ordering (ADR-0003) |
| `scm_zone` | yes (`spike/zone-probe`) |
| `scm_ethernet_interface` (folder scope) | yes (`spike/interface-probe`) |
| `scm_logical_router` | yes, four levels deep (`spike/router-probe`) |
| `scm_ethernet_interface` (device scope) | yes, and isolated (here) |

## Blast radius

* target is the **disconnected** firewall — SCM config reaches a device only on
  push, and a disconnected device cannot be pushed to
* `ethernet1/5` is not one of the two interfaces this tenant uses
  (`$eth-internet` → `ethernet1/3`, `$eth-local` → `ethernet1/4`)
* `var.device` refuses the connected firewall; `var.name` refuses
  `ethernet1/3`, `ethernet1/4`, `$eth-local`, `$eth-internet`. Keep both
  `validation` blocks.

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a
terraform -chdir=spike/device-scope-probe init
terraform -chdir=spike/device-scope-probe plan -out=probe.tfplan   # inspect first
terraform -chdir=spike/device-scope-probe apply probe.tfplan
python3 spike/device-scope-probe/readback.py                       # the real evidence
terraform -chdir=spike/device-scope-probe destroy -auto-approve
```

`readback.py` exits `0` isolated-and-faithful, `3` leaked or dropped, `1` not
found. It reports isolation separately from fidelity, because a leak and a
dropped field need different responses.
