# Logical-router device-scope probe — RESULT RETRACTED

This spike concluded (2026-08-04) that `scm_logical_router` is **folder-scope
only**, because a device-scope create was refused with
`"Device <serial> doesn't exist"` while `device=` worked fine on GET.

**That conclusion is wrong.** The firewall was in a broken registration state.
After it was offboarded and re-onboarded into SCM on 2026-08-05, a device-scope
`scm_logical_router` create was **ACCEPTED**, along with zones, addresses, tags
and security rules — all of which had been refused the same way.

| resource | before re-onboard | after |
|---|---|---|
| `scm_ethernet_interface` | accepted | accepted |
| `scm_zone` | rejected | **accepted** |
| `scm_logical_router` | rejected | **accepted** |
| `scm_address` | rejected | **accepted** |
| `scm_tag` | rejected | **accepted** |
| `scm_security_rule` | rejected | **accepted** |

## The mistake, because it repeated across three spikes

1. **The error was literally true.** `"Device <serial> doesn't exist. Please
   create it before running the command"` meant the device was not properly
   registered for configuration. It was dismissed as misleading because the
   device reported `is_connected: true` and every GET succeeded — read paths and
   config-write paths evidently do not share that registration.

2. **The control was insufficient, not absent.** Each probe used
   `scm_ethernet_interface` as a positive control, and it passed every time —
   because it was the ONE resource that still worked while the device was
   broken. So "interface works, router does not" read as *resource-specific*
   when it was *device partially broken*. **A positive control proves the path
   is alive; it does not prove the environment is healthy**, and it is worth
   least when the passing case is itself the anomaly.

3. **Three spikes agreeing raised confidence without adding evidence.** They
   shared a root cause, so the pattern was one observation repeated, not three.

## What to do differently

When an error names a precondition, **test the precondition directly** rather
than arguing from adjacent evidence that it must be wrong. Here that would have
meant checking whether the device was genuinely registered for config — not
inferring from connectivity and successful reads that it must be.

`main.tf` is kept for re-running; treat its inline commentary as the original
reasoning, not as current fact.
