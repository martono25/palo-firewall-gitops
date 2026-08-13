# ADR-0010 — Address and service objects follow the tag lifecycle

**Status:** accepted (2026-08-13)
**Extends:** [ADR-0009](0009-tag-object-lifecycle.md), whose reasoning this
applies to the objects it did not cover.

## Context

Updating a live rule's destination failed the apply on the pilot on 2026-08-13.
Terraform planned two operations — update the rule in place, and destroy the
address object the old value no longer needed — and ran the **destroy first**:

```
Error deleting addresses / 409 Conflict
errorType: Reference Not Zero
Node cannot be deleted because of references from params:[addr-a102bfc799]
container/[prod-edge]/pre-rulebase/security/rules/[REQ-2026-0809]/destination
```

The rule still pointed at the object because its update had not run — there is
no `Modifying...` line for `scm_security_rule` anywhere in that log. The apply
aborted with the firewall untouched, which is the correct fail-closed outcome
for the wrong reason: nothing was wrong with the change.

**The rule was never being destroyed.** Address and service names are
content-addressed — `addr-` plus the first ten hex of `sha256(value)` — so a
changed value is by construction a different object, not an edited one. That is
deliberate: identical values collapse to one shared object, which is why
`10.20.1.0/24` is a single object referenced by three rules. Editing an object
in place would silently change every rule pointing at it.

So a destination change is: create the new object, repoint the rule, and the old
object becomes unreferenced. Only that last step failed.

### This is Terraform's documented behaviour, not a provider defect

`hashicorp/terraform#32136` records that when a parent is updated, a child is
deleted, and the parent references the child, ordering the update before the
delete is only guaranteed when the child is being **recreated** under
`create_before_destroy`. A pure delete carries no such guarantee.
`hashicorp/terraform#31309` is the same shape. `create_before_destroy` therefore
does not fix this: it inverts edges for replacement, and nothing is being
replaced.

### Why it went unnoticed

It requires an object referenced by exactly one rule. Most are. In `prod-edge`,
six of eight addresses and one of two services are singly-referenced, so **most
rule updates that change a source, destination or service hit this.** An earlier
update the same day passed only because the address it released
(`10.20.1.0/24`) happened to be shared by three other rules and so was never
orphaned.

### ADR-0009 already ruled out the alternatives

Its reasoning transfers unchanged, because the relationship between a rule and
an address is the relationship between a rule and a tag:

* **`-target` the rules** — pulls the object in as a dependency and plans the
  destroy anyway.
* **`depends_on`** — creates the missing edge, and is the pattern this module
  REMOVED once already because it made every rule depend on every object
  instance, so a `destroy -target` of one address cascaded into destroying ALL
  rules. It trades a rare failed apply for a rare catastrophic one.
* **Keep the object in `for_each` so it is never destroyed** — the map is
  computed by the compiler from the current intent tree, so a released object is
  absent from it by definition. Retaining it would require the compiler to know
  Terraform's state, which it deliberately does not (ADR-0004).

## Decision

Address and service objects are **created before the apply and never destroyed
by Terraform**, exactly as tags are:

```
ensure_objects   before apply — create what is missing, touch nothing else
<terraform apply + push>
sweep_objects    after push   — remove objects nothing references
```

The rule update and the garbage collection stop being the same transaction,
which is the actual problem. An orphaned object is inert: it breaks nothing
while it waits for the sweep.

Two safety rules, both **stronger here than for tags**:

* **Only objects this platform minted are swept, and that is proven.** An object
  is ours exactly when its name equals the name its own value hashes to. Tags
  can only be recognised by a name prefix, which anyone can type; a hash
  collision cannot be. Verified against the live tenant: every `addr-*` and
  `svc-*` name in `prod-edge` reproduces from its value.

* **An object is swept only when nothing references it**, and references are
  found by walking every referring object's JSON for the name **anywhere** in
  it, rather than by reading fields we guessed. Over-detecting a reference
  leaves an inert object behind; under-detecting one deletes an object a rule is
  using — the same 409, done deliberately, which is worse. A failed reference
  read sweeps nothing.

## Consequences

**Good.** The failure class disappears rather than being retried around. Rule
updates — the ordinary day-2 operation — stop depending on whether the address
they release happens to be shared. Ownership is provable rather than asserted.

**Costs.** Address and service objects leave Terraform's state, which requires a
one-time `terraform state rm` per object against live infrastructure. Until the
sweep runs, released objects linger; a sweep that fails leaves them indefinitely,
which is untidy but harmless. Two more API round-trips per apply.

**What this does not change.** The compiler stays offline (ADR-0004): it emits
the objects a change needs, and neither ensure nor sweep consults it for
anything but that list. Rules remain Terraform-managed.

## Rollout

Deliberately two changes, because the second touches live state:

1. **The lifecycle code and this ADR** — `ensure_objects` / `sweep_objects`,
   fully covered offline. Changes no behaviour on its own.
2. **The switch** — remove `scm_address` and `scm_service` from the module, have
   rules reference names directly, migrate existing objects out of Terraform
   state, and wire ensure/sweep into the apply. Needs a window and verification
   on the pilot, and is where the risk lives.
