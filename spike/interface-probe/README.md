# interface-probe — does the scm provider WRITE `scm_ethernet_interface`?

ADR-0005 prerequisite 4. Same question as `spike/zone-probe`, different resource:

> Does the provider persist the fields we set, or silently drop them?

Fidelity varies **per resource type** and cannot be inferred. `scm_security_rule`
drops `application` / `profile_setting` / `log_setting` / ordering — the whole
reason `src/fwgitops/enrich.py` exists (ADR-0003). `scm_zone` turned out
faithful. So each new kind gets probed before it is scoped.

## Result (provider v1.0.11, 2026-08-02)

**The provider writes `scm_ethernet_interface` faithfully.** `InterfaceRequest`
is a compiler + tfvars mapping; **no `enrich`-style subsystem needed.**

| Field | Sent | SCM stored |
|---|---|---|
| `comment` (top-level scalar) | `fwgitops fidelity probe…` | same |
| `layer3.mtu` (nested scalar) | `1500` | `1500` |
| `layer3.ip` (nested list-of-objects) | `[{name: 10.99.99.1/30}]` | same |

`layer3.ip` is the load-bearing one — a nested list of objects is the hardest
shape for a provider to round-trip, and it is exactly what interface addressing
needs.

## Why this was safe to run

ADR-0005 assumed there was no clean scratch target, because the real interfaces
live in `ngfw-shared`, which feeds `prod-edge` (2 devices) and `GitOps`. There
is one, and the ADR was too pessimistic:

- **`GitOps` has zero devices**, and nothing inherits *from* it → no device is
  reached.
- A **new name** (`$eth-fwgitops-probe`) is not an override of `$eth-local` or
  `$eth-internet` → nothing existing is shadowed or modified.

The fidelity question does not care which folder it is asked in.

`main.tf` carries a `validation` block that **refuses** `prod-edge`,
`ngfw-shared` and `All` — verified: both are rejected before a plan runs. Do not
remove it.

## Run

```bash
set -a; source ~/.fwgitops/scm.env; set +a

cd spike/interface-probe && terraform init && terraform apply
cd - && python3 spike/interface-probe/readback.py --folder GitOps
cd spike/interface-probe && terraform destroy
```

`readback.py` exits 3 when a field was dropped. Verified after destroy that all
three folders show only `$eth-internet` and `$eth-local`.

## Reuse for the next kind

Swap the resource block and the `INTENDED` map. `RouteRequest` and `NatRequest`
each need the same answer, and each may differ.
