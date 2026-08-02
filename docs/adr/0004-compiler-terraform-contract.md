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

A **third hole**, found by the ship red-team pass, was also live in v1.0 and is
the sneakiest of the three. Terraform's object-to-object conversion **silently
discards attributes the target type does not declare**. The root's
`security_rules` type omitted the six ADR-0003 attributes the module declares and
the compiler emits, so `application`, `profile_group`, `log_setting`, `rulebase`,
`relative_position` and `target_rule` never crossed the root boundary and rules
were built with the module's `["any"]` default instead of the intent's App-ID.

Hole 3 is invisible to a key-level check: the key `security_rules` is both
declared and wired. The file's own comment claimed the types were "kept in sync
with `src/fwgitops/compiler.py`" — they were not, and nothing enforced it.

| Hole | Terraform's signal |
|---|---|
| 1 — tfvars key with no matching `variable` | warning, **exit 0** |
| 2 — `variable` declared but never referenced | **no diagnostic at all** |
| 3 — object type omits an emitted attribute | **silently discarded** |

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

1. **`tfcontract.py`** — pure-Python checks for all three holes. No Terraform
   binary, no cloud credentials, runs in milliseconds. Hole 1: every emitted
   top-level tfvars key must have a matching `variable` declaration. Hole 2:
   `var.<key>` must actually be referenced somewhere in the root (broader than
   "a module argument of that name", so a differently-named argument or a bare
   `resource` still counts). Hole 3: `declared_object_attributes` parses the
   variable's `object({...})` type and asserts every attribute the compiler
   emits for that key is declared.
2. **Compile fails closed.** `run_compile` plans every output file, checks the
   contract, and writes nothing if it is violated (exit 2) — preserving the
   all-or-nothing guarantee across folders, not just within one. A **missing**
   Terraform root is itself a violation: gating the check on "does a Terraform
   root exist" made the check that catches missing Terraform skip exactly when
   Terraform was missing, and both CI loops then skipped the folder too
   (`[ -f "$dir/main.tf" ] || continue`), so the plan and its grep never ran
   either. Scratch use opts out by name via `--allow-missing-root`.
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
- All three silent holes are covered, including the one with no diagnostic and
  the one where the key looks perfectly wired.
- The root and module object types can no longer drift apart unnoticed — a
  repo-tree test compiles the real intents and asserts every emitted attribute
  is declared, which is what the "kept in sync" comment only claimed.
- A real plan failure now fails the PR — previously it never did.
- Rules may reference any zone that genuinely exists.

**Negative / cost**
- `tfcontract.py` is a small regex + brace parser, not a full HCL parser. It is
  adequate for this repo's hand-written root modules and would need revisiting
  if they grew generated or dynamic module blocks. String literals are masked
  before any structural scan (a `}` inside a string once collapsed brace depth;
  a `//` inside a URL once ate a closing brace), and line-break characters are
  never masked, because the comment pass zips the masked and original text line
  by line.
- Hole 3's check only inspects the TOP level of an `object({...})` type. A
  nested `optional(object({...}))` whose inner attributes drift is not covered.
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

### Read-only findings that reshape `InterfaceRequest` (2026-08-02)

Discovery against the live tenant, before any write:

- **This tenant does not name interfaces literally.** The zones `local` and
  `internet` reference `$eth-local` / `$eth-internet` — SCM **variables**, each
  an object with a `default_value` (e.g. `ethernet1/3`) defined in the parent
  folder `ngfw-shared` and inherited down. So `ZoneSpec.interfaces` values like
  `ethernet1/2` (used in our fixtures) are the wrong shape here.
- **This changes A4's premise.** Validating interface names against a device's
  *physical* interfaces validates the wrong vocabulary; the real one is the
  inherited `$eth-*` variable set, which lives in an ancestor folder. Any
  interface catalog must understand folder inheritance.
- **Four of the seven zones carry no interfaces at all** (`layer3: []`), and
  `proxy` has no `network` block. That is the concrete "zone that carries no
  traffic" state, on the live tenant.
- **`scm_ethernet_interface` has no `tag` attribute either**, like `scm_zone`.
  Only 14 of the provider's resources are taggable, so the tag-based drift model
  in `drift.py` structurally covers a minority of object types — this is not a
  zone-specific quirk.

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
