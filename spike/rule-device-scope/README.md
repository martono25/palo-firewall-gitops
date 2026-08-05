# Security-rule device-scope probe — RUN 2026-08-05. RESULT: FOLDER-SCOPE ONLY

The question v2.0 rule provisioning turns on: **can a rule target one firewall?**

## Result: no

| | scope | result |
|---|---|---|
| `scm_ethernet_interface` **(control)** | `device = 007955000894453` | **created**, id `f13fe5c9` |
| `scm_security_rule` | `device = 007955000894453` | **REJECTED** `API_I00013` |

Same firewall, same provider (`1.0.12-beta.4`), same credentials, same apply.
The interface succeeded and the rule did not, so this is **resource-specific, not
a property of the device or of device scope**.

```
API_I00013  Operation Impossible
"Device 007955000894453 doesn't exist. Please create it before running the command"
```

The device exists, is connected, and `GET .../security-rules?device=<serial>`
returns its inherited rulebase. The message is wrong under every reading — which
is exactly why the control was mandatory. Without it, "device-scope rule
rejected" and "device scope rejected" are indistinguishable, and they imply
different builds.

## Three of four now behave this way

| resource | device scope on write | probe |
|---|---|---|
| `scm_ethernet_interface` | **works** | `spike/device-override-probe` |
| `scm_logical_router` | refused | `spike/router-device-naming` |
| `scm_zone` | refused | `spike/zone-device-scope` |
| `scm_security_rule` | refused | this one |

Only the interface supports it. **Folder-only is the correct default assumption
for an SCM network/policy resource**; device scope is the exception and must be
probed before anything is built on it.

Each of the three documents `device` in the provider registry, and each refuses
it at write time with the same misleading message. This is a documented-vs-actual
gap in the SCM API, **not a provider defect** — the identical raw REST call fails
identically with no Terraform involved, so the provider is faithfully surfacing
the API's answer.

## What it means for v2.0

**Per-firewall rule targeting is not available.** A rule reaches a firewall only
by folder inheritance, so the targeting model stays:

```
environment → folder → every firewall beneath it
```

That closes one design option and simplifies the rest: there is no point adding
`device:` to `AccessRequest`, and any requirement for firewall-specific policy
has to be met by putting that firewall in its own folder — which makes **folder
granularity the unit of policy isolation**, and makes re-parenting (already
deferred to v2.0) the mechanism that matters.

## Blast radius

* local state; cannot touch the S3-backed roots
* the rule was `disabled = true` — it could not match a packet even if pushed
* `action = deny` with RFC 5737 documentation ranges (`198.51.100.0/24` →
  `203.0.113.0/24`), absent from this estate, so it matched nothing even enabled
* never pushed; SCM config reaches a device only on push
* control was MTU-only on `ethernet1/1`, which the real device root does not
  manage and which has no zone or address
* destroyed immediately; readback confirms no probe rule at either scope, only
  `ethernet1/2..4` on the device, and `terraform/device-007955000894453` plans
  `No changes`

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a
terraform init
terraform apply -var run_rule=false   # CONTROL alone — should SUCCEED
terraform apply                       # + the rule — should FAIL API_I00013
terraform destroy -var run_rule=false
```
