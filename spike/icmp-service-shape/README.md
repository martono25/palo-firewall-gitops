# ICMP service shape — RUN, PASSED (2026-08-09)

`Service` in the intent model is `protocol` + `port`, tcp/udp only, so **ping is
unrequestable** (TODOS: "An AccessRequest cannot express ICMP"). PAN-OS matches
ICMP by APPLICATION, not by a port-based service — and `scm_service` requires a
port, so ICMP cannot be a service object at all.

That leaves one question the intent model turns on: **what goes in `service` on
the rule itself?**

## Result

| variant | sent | outcome |
|---|---|---|
| **A** | `application: [ping]`, **no `service` key** | **REJECTED 400** |
| **B** | `application: [ping]`, `service: [any]` | **created**, read back identical |
| **C** | `application: [ping]`, `service: [application-default]` | **created**, read back identical |
| **D** *(control)* | `application: [any]`, `service: [svc-fd64e1b89a]` | created — the POST path was alive in this run |

```
A: 400 API_I00035  Invalid Request Payload  details: ["\"service\" is required"]
```

## The finding that matters beyond ICMP

**The provider schema says `service` is OPTIONAL. SCM says it is REQUIRED.**

    $ terraform providers schema -json
    scm_security_rule.service: type=["list","string"] optional=true

    $ POST /config/security/v1/security-rules   (no service key)
    400  "service" is required

Both statements are evidenced above — the schema dump is local and offline, the
400 is from this tenant. So a rule that Terraform would plan happily is refused
by the API, and "optional in the schema" cannot be read as "omittable". Recorded
because this project has twice reasoned from a provider's declared shape to what
SCM will accept, and been wrong both times.

## Which shape to use

**`application-default` (C), not `any` (B).** Both are accepted; they are not
equivalent:

* `service: [any]` — the `ping` App-ID is matched on ANY protocol/port. App-ID
  still gates it, so this is not wide open, but the rule permits more than it
  says.
* `service: [application-default]` — restricted to the application's own default
  ports/protocol. For `ping` that is ICMP echo, which is exactly what the
  requester asked for and nothing else.

`application-default` is the tighter of two verified options, so it is the one
to build on. This is a RECOMMENDATION from the probe, not a measurement of
enforcement: what a device does with each was NOT tested here — the probe never
pushed, so nothing reached a firewall.

## Safety of this probe

Fail-safe by construction, and worth stating because it wrote to a live tenant:

* **Never pushed.** Writes landed in the candidate only, so nothing reached a
  firewall.
* **Every rule was `deny`.** Had a stray push committed one, it would have
  removed access, not granted it — and ping is not permitted today anyway, so
  strictly no new access was possible.
* **Cleanup was verified, not assumed.** All three created rules deleted, the
  folder re-listed, `leftover_after_cleanup: []`, and the rule count back to its
  starting 9.

## Control caveat, restated deliberately

Variant D passed in the same batch, which proves the POST path was alive. It does
**not** prove the environment was healthy. That distinction is the whole reason
`spike/rule-device-scope` had to be retracted on 2026-08-05: its control was the
one resource still working while the device registration was broken, so
"interface works, rule does not" read as resource-specific when it was
device-broken. A control proves the path is alive; nothing more.

## What this does NOT answer

* **What the device enforces.** Nothing was pushed, so PAN-OS behaviour for
  either shape is untested on hardware.
* **Mixing.** Whether one rule may carry `application-default` alongside a
  specific service object was not tested — and the intent model should refuse to
  mix ICMP with tcp/udp in one request regardless, because `service` is a
  rule-level list and mixing changes what the other entries mean.
