# zone-probe — does the scm provider actually WRITE what you tell it?

Answers one question, for one resource type, with evidence:

> Does `paloaltonetworks/scm` persist the fields we set, or silently drop them?

That question is not paranoia. On `scm_security_rule` the provider **accepts**
`application`, `profile_setting`, `log_setting` and ordering, reports success,
treats them as computed, and never writes them (ADR-0003, verified live
2026-07-28). `src/fwgitops/enrich.py` exists entirely to work around it. So
before modelling fields on any new resource, probe it.

**Terraform reporting success proves nothing. Only the read-back counts.**

## Result for `scm_zone` (provider v1.0.11, 2026-07-31)

The provider writes zone fields **faithfully** — the rule defect does NOT apply
here, so zones need no `enrich`-style subsystem.

| Field | Sent | SCM stored |
|---|---|---|
| `enable_user_identification` | `true` | `true` |
| `enable_device_identification` | `true` | `true` |
| `network.log_setting` | `Cortex Data Lake` | `Cortex Data Lake` |
| `network.zone_protection_profile` | `best-practice` | `best-practice` |

Second result: **SCM reference-validates zone fields, fail-closed.** A bogus
`network.log_setting` was rejected at create:

```
API_I00013 ... 'fwgitops-does-not-exist-xyz' is not a valid reference ...
type:INVALID_REFERENCE
```

Nothing was created. See ADR-0004.

## Run it

Needs SCM credentials only — **no firewall, no EC2, no cost.** SCM objects live
in folders independently of any bound device; a device is only needed for
*push*.

```bash
set -a; source ~/.fwgitops/scm.env; set +a   # never paste secrets into a terminal you share

python3 spike/zone-probe/discover.py          # READ-ONLY: folders, zones, profiles

cd spike/zone-probe && terraform init
terraform apply -var 'folder=GitOps' -var 'zone_protection_profile=best-practice'

cd - && python3 spike/zone-probe/readback.py \
  --folder GitOps --expect-network 'zone_protection_profile=best-practice'

cd spike/zone-probe && terraform destroy -var 'folder=GitOps'
```

Use a scratch folder (`GitOps`), never `prod-edge`. The probe zone has an empty
interface list, so it is inert — it cannot carry or affect traffic. State is
local to this directory and cannot touch the S3-backed `prod-edge` state.
`readback.py` exits 3 when a field was dropped.

## Reuse it for the next kind

Fidelity varies **per resource type** — rules are broken, zones are fine. It
cannot be generalised. Before scoping `InterfaceRequest`, point this at
`scm_ethernet_interface`: swap the resource block in `main.tf` and the expected
fields in `readback.py`.

## Endpoints that cost time to find

`403` and `400` here mean a malformed request far more often than a permissions
problem. Most SCM config endpoints are folder-scoped and return 400 or empty
without a `folder` param. Check <https://pan.dev/scm/docs/home/> before blaming
the account.

| Object | Path | Note |
|---|---|---|
| profile groups | `/config/security/v1/profile-groups` | `objects/v1` returns 403 |
| zone protection profiles | `/config/network/v1/zone-protection-profiles` | **requires** `folder` |
| zones | `/config/network/v1/zones` | requires `folder` |
| log forwarding | `/config/objects/v1/log-forwarding-profiles` | lists predefined built-ins too |

The log-forwarding listing includes predefined objects (`Cortex Data Lake`,
`IoT Security Default Profile`) that are valid API references but are **not**
selectable in the SCM UI. Do not treat that listing as the set of sanctioned
names — `catalog/log-forwarding.yaml` listing only `log-best` is correct.
