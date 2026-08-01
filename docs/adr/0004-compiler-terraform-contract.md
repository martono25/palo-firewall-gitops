# ADR-0004 — The compiler → Terraform contract must be enforced, not assumed

- **Status:** Accepted (built)
- **Date:** 2026-07-31
- **Deciders:** Martono, Claude

## Context

`ZoneRequest` (kind #2, ADR-0001) shipped as a **silent dead end**. The compiler
wrote `terraform/<folder>/zones.auto.tfvars.json` on every run, but
`terraform/prod-edge/variables.tf` declared no `zones` variable and the module
had no `scm_zone` resource.

Terraform treats an auto-loaded `*.auto.tfvars.json` that sets an **undeclared**
variable as a *warning* and exits **0**. So:

```
compile  -> exit 0, file written
plan     -> exit 0, "Warning: Value for undeclared variable"
apply    -> exit 0, nothing created
device   -> unchanged
CI       -> green
```

This survived a full v1.0 release, live hardware validation, and 327 tests. The
test suite could not have caught it: every zone test asserted the compiler wrote
the right JSON and stopped there — exactly where the failure begins.

Investigating turned up a **second, quieter hole**. A variable that IS declared
in `variables.tf` but never passed into the `module` block produces **no
diagnostic at all**. `terraform/prod-edge/main.tf` passes four arguments; adding
a fifth variable without wiring it would be silently ignored with not even a
warning to grep for.

Two further defects surfaced while verifying the above:

- `.github/workflows/pr-validate.yml` piped `terraform plan` through `tee` and
  appended `|| true`, so **every** plan failure was swallowed and
  `-detailed-exitcode` was meaningless (the exit code was `tee`'s). This
  affected shipped security rules, not just zones.
- `catalog/environments.yaml` declared two baseline zones (`local`, `internet`)
  while the folder actually carries seven (verified live). Since
  `check_zone_consistency` validates against that set, a rule referencing a real
  zone such as `proxy` was **rejected at compile time as undeclared** —
  fail-closed machinery producing a false negative.

## Decision

**Enforce the contract in code, at both ends, rather than fixing the one
instance.**

1. **`tfcontract.py`** — pure-Python checks for both holes. No Terraform binary,
   no cloud credentials, runs in milliseconds. Hole 1: every emitted top-level
   tfvars key must have a matching `variable` declaration. Hole 2: that variable
   must also be passed to a `module` block.
2. **Compile fails closed.** `run_compile` plans every output file, checks the
   contract, and writes nothing if it is violated (exit 2) — preserving the
   existing all-or-nothing guarantee. Enforced only where a Terraform root
   actually exists, so scratch directories remain usable.
3. **CI fails on the warning.** The plan step captures the plan's own exit
   status (treating `2` = changes present as normal for a PR) and fails the job
   if the output contains `Value for undeclared variable`.
4. **`baseline_zones`** — the env map gains an optional list so it can name every
   zone that exists on a device, not just the default pair.
5. **`check_zone_collisions`** — a `ZoneRequest` naming an existing device zone
   is rejected. `check_zone_consistency` *unions* baseline and declared zones, so
   such a request looks maximally valid there while Terraform would try to
   create over a live zone.

### Why not just add the `scm_zone` resource

That fixes today's instance and leaves the mechanism intact.
`InterfaceRequest`, `RouteRequest` and `NatRequest` each add a new top-level
variable and each can be forgotten the same silent way. The bug is not "someone
forgot the zones variable" — it is "forgetting is invisible."

## Consequences

**Positive**
- A kind wired into the compiler but not into Terraform now fails loudly at
  compile time and in CI, instead of passing green forever.
- Both silent holes are covered, including the one with no diagnostic.
- A real plan failure now fails the PR — previously it never did.
- Rules may reference any zone that genuinely exists.

**Negative / cost**
- `tfcontract.py` is a small regex + brace parser, not a full HCL parser. It is
  adequate for this repo's hand-written root modules and would need revisiting
  if they grew generated or dynamic module blocks.
- The compile-time check reads `.tf` files, a new coupling between the compiler
  and the Terraform layout.

## Verified against the live tenant (2026-07-31)

Probed on provider `paloaltonetworks/scm` v1.0.11 — an inert zone (empty
interface list) created in folder `GitOps`, read back over the SCM REST API,
then destroyed:

- **The provider writes `scm_zone` fields faithfully.**
  `enable_user_identification`, `enable_device_identification`,
  `network.log_setting` and `network.zone_protection_profile` all round-tripped.
  The computed-attribute drop that breaks `scm_security_rule` (ADR-0003) does
  **not** apply to zones, so **zones need no `enrich`-style workaround**.
- **SCM reference-validates zone fields, fail-closed.** A bogus `log_setting`
  was rejected at create with `API_I00013 … type:INVALID_REFERENCE`.
- **Fidelity varies per resource type.** It cannot be generalised — re-run the
  probe against `scm_ethernet_interface` before scoping `InterfaceRequest`.

Useful endpoints (several earlier `403`/`400` results were malformed requests,
not permissions — most SCM config endpoints require a `folder` param):

| Object | Path |
|---|---|
| profile groups | `/config/security/v1/profile-groups` |
| zone protection profiles | `/config/network/v1/zone-protection-profiles` |
| zones | `/config/network/v1/zones` |
| log forwarding | `/config/objects/v1/log-forwarding-profiles` |

Note the log-forwarding listing includes **predefined** objects (`Cortex Data
Lake`, `IoT Security Default Profile`) that are not selectable in the UI though
they are valid API references — do not treat that listing as the set of
sanctioned names. Always check a request shape against
<https://pan.dev/scm/docs/home/> before concluding an error means missing
permissions.

## Related
- ADR-0001 — the kind registry this bug exposed as incomplete; also records why
  `drift` cannot be made kind-agnostic (`scm_zone` has no `tag` attribute).
- ADR-0002 — Day-1 ordering; `InterfaceRequest` is the real prerequisite for a
  traffic-carrying zone.
- ADR-0003 — the `scm_security_rule` provider defect that `enrich` works around,
  and the base-rate prediction the zone probe disproved.
- `TODOS.md` — deferred zone-model work, with the reasoning that deferred it.
