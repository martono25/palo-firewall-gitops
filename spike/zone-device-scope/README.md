# Zone device-scope probe — RUN 2026-08-05. RESULT RETRACTED (see below)

---

# RETRACTED 2026-08-05 — THE CONCLUSION BELOW IS WRONG

The firewall was in a broken registration state. After it was offboarded and
re-onboarded into SCM, **every resource accepts a device-scope write**:

| resource | before re-onboard | after |
|---|---|---|
| `scm_ethernet_interface` | accepted | accepted |
| `scm_zone` | rejected | **accepted** |
| `scm_logical_router` | rejected | **accepted** |
| `scm_address` | rejected | **accepted** |
| `scm_tag` | rejected | **accepted** |
| `scm_security_rule` | rejected | **accepted** |

Reproduced three times with readback and cleanup.

**Why the control did not save me.** The control was `scm_ethernet_interface` —
the ONE resource that still worked while the device was broken. A positive
control is only decisive if the positive case is normal; here it was the
anomaly, so "interface works, zone does not" read as *resource-specific* when it
was really *device partially broken*. A control proves the path is alive, not
that the rest of the system is healthy.

**The error message was literally true.** `"Device <serial> doesn't exist.
Please create it before running the command"` meant exactly what it said: the
device was not properly registered for configuration. It was dismissed as
misleading because the device showed `is_connected: true` and every GET worked —
read paths and config-write paths clearly do not share that registration.

**What to do differently:** when an error names a precondition, test the
precondition directly instead of arguing from adjacent evidence that it must be
wrong.

The text below is preserved as written, not corrected in place, so the reasoning
that led to the wrong conclusion stays legible.

---


Asked because `ZoneRequest` has been built since v1.2.0 and has **never reached
a firewall**, and the obvious way to make one land on a specific device is to
target that device.

## Result

| | scope | result |
|---|---|---|
| `scm_zone` | `device = 007955000894453` | **REJECTED** `API_I00013` |
| `scm_ethernet_interface` **(control)** | `device = 007955000894453` | **created**, id `90ead44c` |

Same device, same provider (`1.0.12-beta.4`), same credentials, same apply.
Zones are refused where interfaces are accepted, so this is **resource-specific,
not a property of the device or of device scope**.

The error is the misleading one seen before:

```
API_I00013  Device 007955000894453 doesn't exist.
            Please create it before running the command
```

The device exists, is connected, and `GET .../zones?device=<serial>` returns its
full inherited zone list. The message is wrong under every reading, which is
exactly why the control was mandatory: without it, "device-scope zone rejected"
and "device scope rejected" are indistinguishable, and they imply different
builds.

**Same shape as `scm_logical_router`** (`spike/router-device-naming`): accepted
on GET, refused on POST, with a message that blames the device. Two of the four
Day-1 resources now behave this way, so *treat folder-only as the default
assumption for a network resource and probe before relying on device scope.*

## This contradicts the documentation, and that is the finding

The registry page for `scm_zone` states:

> Note: You must specify exactly one of device, folder or snippet.

and documents the device import form `terraform import scm_zone.example
::device:id`. Docs were read **before** running this, precisely so the result
would not turn into another unfounded provider accusation.

So the discrepancy is documented-vs-actual, and it is **not** a provider defect:
the provider sends the request and surfaces the API's response faithfully — the
identical raw REST call fails the identical way with no Terraform involved. The
gap is between the SCM API and the docs generated from its schema. Recorded as a
discrepancy, not a bug, and not attributed to the provider.

## What it means for the platform

`ZoneRequest` is **folder-scope only**, like `RouteRequest`. `intent.py` should
pass `allow_device=False` for it, the same as routers, so a `device:` zone
intent is rejected at PR time with a useful message instead of failing at apply
with "Device doesn't exist" — which sends the reader hunting for a missing
device.

That is a real gap today: `_load_zone_spec` calls `_load_target` with the
default `allow_device=True`, so this intent compiles clean and dies at apply.

## Blast radius

* local state; cannot touch the S3-backed roots
* `layer3 = []` on the zone — binds no interface, so it cannot pull a live
  interface out of `local`/`internet`
* control was MTU-only on `ethernet1/2`, an interface with an ENI but no PAN-OS
  config and no zone; no `ip` was set
* both destroyed immediately; `device=` readback confirms only `ethernet1/3` and
  `ethernet1/4` remain, with their real addresses untouched

## Re-running

```bash
set -a; source ~/.fwgitops/scm.env; set +a
terraform init
terraform apply -var run_zone=false   # control alone — should SUCCEED
terraform apply                       # + the zone — should FAIL API_I00013
terraform destroy -var run_zone=false
```
