# ADR-0009 — Terraform creates tag objects; it does not destroy them

- **Status:** Accepted (2026-08-10)
- **Date:** 2026-08-10
- **Deciders:** Martono, Claude
- **Supersedes:** nothing. **Depends on:** ADR-0004 (compiler→Terraform contract)

## Context

Changing a tag VALUE on a live rule fails the apply.

MEASURED 2026-08-10, `spike/tag-destroy-ordering`. Phase 2 changed one
`gitops:ticket:` value. Terraform planned all three actions correctly — update
the rule's tag list, create the new tag, destroy the old one — and then ran the
**destroy before the update**:

```
scm_tag.this["gitops:ticket:PROBE-AAAA"]: Destroying...
Error: 409 Conflict — Reference Not Zero, NON_ZERO_REFS
  [container -> prod-edge -> pre-rulebase -> security -> rules
     -> PROBE-TAGORDER -> tag]
```

The rule update never ran, with `-parallelism=1`, so this is ordering rather than
a race. Once the rule's config no longer REFERENCES the old tag, nothing orders
the destroy after the update: **the edge that existed for creation is gone
exactly when it is needed for destruction.**

**Narrow, and confirmed narrow.** It fires only where the rule is UPDATED in
place. Removing a whole rule orders correctly — measured 2026-08-09 retiring
`REQ-2026-07302`, where the rule was destroyed before its tag — because
destroying the rule keeps the edge. So the trigger is a corrected ticket number,
not a deletion.

### What was ruled out, and why

* **`-target` the rules.** Pulls the tag in as a dependency and plans the destroy
  anyway. Recorded in TODOS from the 2026-08-05 incident.
* **`depends_on = [scm_tag.this]` on the rules.** Creates the missing edge — and
  is the pattern this module REMOVED once already, because it made every rule
  depend on every object instance, and a `destroy -target` of one address
  cascaded into destroying ALL rules. Reintroducing it trades a rare failed
  apply for a rare catastrophic one.
* **Keeping the tag in `for_each` so it is never destroyed.** The map is computed
  by the compiler from the current intent tree; a retired tag value is absent
  from it by definition. Retaining it would require the compiler to know
  Terraform's state, which it deliberately does not — the compiler is offline
  (ADR-0004).

## Decision

**Terraform CREATES tag objects. It never DESTROYS them.**

1. `scm_tag` leaves the module. Tags are created by **`fwgitops ensure-tags`**
   before apply — idempotent: read what exists, create only what is missing.
2. Unreferenced tags are removed by **`fwgitops sweep-tags`**, a separate step
   AFTER the push, never in the same operation as the rule change that released
   them.
3. Existing tag objects are dropped from state with a `removed` block carrying
   `lifecycle { destroy = false }`, so Terraform FORGETS them rather than
   destroying them. Without it the first apply after this change would try to
   destroy every tag and 409 on all of them at once.
4. **Only `gitops:`-prefixed tags are swept, and only when nothing references
   them.** A tag this platform did not create is never touched.

## Consequences

**An unreferenced `gitops:` tag is now EXPECTED, not a finding.** It exists
between the apply that released it and the sweep that removes it, and indefinitely
if the sweep is skipped.

Drift needs no change to tolerate that, and this ADR said it did until it was
checked: nothing in `drift.py` or `catalogcheck.py` enumerates tag OBJECTS. The
tag-based engine reads the `tag` ATTRIBUTE of rules to decide what is managed; the
object list is never fetched. So an orphaned tag was always invisible.

**Which cuts the other way too, and is the real cost:** if the sweep stops
running, nothing reports the accumulating garbage. No check covers it, and this
decision does not add one. Accepted deliberately — an inert object nobody
references is a smaller problem than an apply that fails on a ticket-number
correction — but it is a gap, not an absence of one.

**Tag creation is no longer visible in `terraform plan`.** A reviewer reading a
PR's plan will not see "3 tags will be created". The tags a change needs are
derivable from the intent, and the evidence bundle records them, but the plan is
no longer the whole story for tags. Stated because ADR-0004 exists precisely to
keep the plan honest about what will happen.

**The sweep is a separate failure surface.** If it errors the apply has already
succeeded, so it must not fail the pipeline — it warns. A sweep that never runs
leaves garbage; a sweep that fails the run would turn a cosmetic problem into an
outage.

**This does not fix ordering in general.** It removes the one case that bites.
Any future object type that is both referenced and destroyed in the same apply
has the same exposure, and will need the same treatment or its own measurement.

## Alternatives considered

**Reject the change at compile time** — fail when a tag value change would orphan
a referenced tag, telling the requester to split it into two PRs. No architecture
change, no state migration. Rejected: it pushes a tooling defect onto the
requester, and "correct the ticket number" becoming a two-PR dance is exactly the
friction this platform exists to remove.

**Stop tagging tickets on rules.** The 409 only happens because
`gitops:ticket:<id>` embeds a MUTABLE value in an object name. Dropping it
removes the whole class. Rejected for now: finding rules by ticket in the SCM UI
is the main reason a responder opens the UI at all, and `fwgitops where` does not
help someone who is not in the repo. Worth revisiting if the sweep proves
troublesome.

**Do nothing, document the workaround.** It has fired once, during a migration.
Rejected: the recovery (`terraform state rm`, apply, delete the orphan by API) is
a runbook nobody will find at the moment they need it, and the trigger — a
corrected ticket number — is ordinary.
