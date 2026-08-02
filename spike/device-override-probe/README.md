# Device-scope override probe — RUN, PASSED (2026-08-02)

Writing an **existing inherited** interface at `device=` scope: per-device
override, or mutation of the object every device inherits?

`spike/device-scope-probe` proved `device=` isolates for a **new** object. It
said nothing about this case, because the interfaces this tenant actually uses
project the same object id at both scopes:

```
folder=ngfw-shared   $eth-local (default_value: ethernet1/4)   35479f59-…
device=<serial>      ethernet1/4                               35479f59-…
```

ADR-0005 has `InterfaceRequest` configuring interfaces that already exist, so
this is the path it actually takes.

## Result: per-device override, fully isolated

MTU-only write to `ethernet1/4` at `device=007955000893662` (disconnected).
Every scope diffed against a baseline captured before the apply:

| scope | object | before | after |
|---|---|---|---|
| `device=…3662` **(target)** | `ethernet1/4` | `35479f59` `layer3:{}` | **`7fa1be02`** `layer3:{mtu:1476}` |
| `device=…4453` (other firewall) | `ethernet1/4` | `35479f59` `layer3:{}` | unchanged |
| `folder=ngfw-shared` | `$eth-local` | `35479f59` `layer3:{}` | unchanged |
| `folder=prod-edge` | `$eth-local` | `35479f59` `layer3:{}` | unchanged |

The write created a **new object** (`7fa1be02`) that shadows the inherited one
for that device only. Nothing else moved.

**So `device:` targeting CAN isolate interface addressing** — the change most
likely to break connectivity — and is safe to build on.

## Bonus finding: deletion reverts to inheritance

After `terraform destroy`, the target's `ethernet1/4` went back to id
`35479f59` with `layer3: {}` — the inherited object, intact. Removing an
override does not leave a hole; it restores inheritance. That is the deletion
semantics `InterfaceRequest` needs, and it came free with the revert check.

Every scope matched the baseline exactly afterwards (`readback.py
--expect-restored`).

## Blast radius

This probe genuinely touches an object `prod-edge` inherits, so the controls
matter:

* target is the **disconnected** firewall. SCM config reaches a device only on
  **push**, and a disconnected device cannot be pushed to. This probe never
  pushes.
* **MTU only, never addressing.** `layer3.ip` stays empty, so even reaching a
  device could not change what the interface answers on. `1476` is distinctive.
* the revert target is exact and captured as a baseline **before** applying, so
  restoration is demonstrated rather than assumed.
* `var.device` refuses the connected firewall. Keep that `validation` block, and
  do not add `ip` to this config.

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a

# 1. baseline FIRST — restoration is checked against it
python3 - <<'PY'
import sys, json; sys.path.insert(0, "src")
from fwgitops.scmapi import ScmCredentials, ScmSession
s = ScmSession(credentials=ScmCredentials.from_env())
scopes = {"dev-target": {"device": "007955000893662"},
          "dev-other": {"device": "007955000894453"},
          "folder-shared": {"folder": "ngfw-shared"},
          "folder-prod": {"folder": "prod-edge"}}
base = {}
for k, p in scopes.items():
    p = dict(p); p["limit"] = 200
    base[k] = s.request("GET", "/config/network/v1/ethernet-interfaces", params=p)["data"]
json.dump(base, open("/tmp/ovr/baseline.json", "w"), indent=1, sort_keys=True)
PY

terraform -chdir=spike/device-override-probe init
terraform -chdir=spike/device-override-probe plan -out=probe.tfplan   # inspect first
terraform -chdir=spike/device-override-probe apply probe.tfplan
python3 spike/device-override-probe/readback.py --baseline /tmp/ovr/baseline.json
terraform -chdir=spike/device-override-probe destroy -auto-approve
python3 spike/device-override-probe/readback.py --baseline /tmp/ovr/baseline.json --expect-restored
```

`readback.py` exits `0` override-and-isolated (or fully restored, with
`--expect-restored`), `3` mutated / not restored, `1` API failure.

## The record — six for six

| probe | question | answer |
|---|---|---|
| `scm_security_rule` (ADR-0003) | writes fields? | **NO** — needs `enrich` |
| `spike/zone-probe` | writes fields? | yes |
| `spike/interface-probe` | writes fields? | yes |
| `spike/router-probe` | writes nested routes? | yes, 4 levels deep |
| `spike/device-scope-probe` | is `device=` isolating? | yes, for new objects |
| `spike/device-override-probe` | override or mutate? | **override**, isolated |

Six for six at answering something inference would have guessed at — twice now
the guess would have been wrong. Probe before building.
