# ADR-0012 — Reconstruct actions, never detections

**Status:** accepted (2026-08-16)

## Context

Two directories hold records of unauthorised change, and both have now had gaps
that somebody wanted to fill by hand:

- `evidence/manual-actions/` — `fw-manual-action/v2`, what was DONE about it.
- `evidence/violations/` — `fw-violation/v1`, what was FOUND.

Three cases arose on 2026-08-16, and each was argued from scratch:

1. **A deletion whose record crashed.** `delete-scm-object.yml` removed
   `test-unmanaged-2` from SCM and then raised `NameError: name '_json' is not
   defined`. The rule was gone; the workflow wrote nothing. The record was
   reconstructed from the run log.
2. **A re-stack that recorded nothing at all.** The `prod-edge` rulebase was
   re-ordered by an apply, five rules re-seated, and no record existed because
   ordering evidence had not been built yet. Reconstructed on the same grounds —
   and it was nearly left out on the reasoning that it was "an authorised
   adoption step rather than someone's unauthorised edit", which is a
   rationalisation: the deletion was authorised too.
3. **The finding behind that re-stack.** The rulebase genuinely WAS out of
   order — probe run 31936407409 read `0727, 0726, 0725` from SCM beforehand —
   but no `reordered` violation record exists, because the recorder shipped
   afterwards. It was proposed that this be reconstructed too, "for
   consistency".

The third proposal was wrong, and the reason it looked right is worth keeping.

## Decision

**An ACTION record may be reconstructed. A DETECTION record may not.**

A `fw-manual-action` asserts *"this change was made"*. That is a fact about the
world, true regardless of who was watching or whether the writer crashed. It can
be established afterwards from a run log, an API read, or a device
configuration, and the record can say where it came from.

A `fw-violation` asserts *"this was OBSERVED, by this run, at this time"*. Every
field says so — `first_seen`, `first_seen_run`, `last_seen_run` all point at the
CI run that did the observing. Creating one after the fact means inventing a run
URL or leaving it null, and either way the record claims a detection that never
happened. **A control that fabricates its own detection history has stopped
being evidence.**

So when a control ships alongside the action it was meant to observe, the
resulting action record has no finding to link, and must say so in
`unlinked_reason` rather than acquire a manufactured one.

Every reconstruction carries `provenance: "reconstructed"` and a
`reconstructed_from` naming its source — enforced at write time, since a record
on disk is already evidence (ADR-0009 addendum, `fw-manual-action/v2`).

## Consequences

The evidence contains one real unauthorised-change event with a remediation and
no finding. That is expected to draw an assessor's eye, and the honest answer is
better than a tidy one: the fact is attested in the action record's `reason` and
`reconstructed_from`, which cite the probe run that read the out-of-order
rulebase from SCM. The trail leads to independent evidence rather than to a
record this platform wrote about itself.

`fw-violation` therefore needs no `provenance` field. It cannot be
hand-authored, so there is no hand-authored case to mark — the guarantee comes
from the record being impossible to create outside the detector, not from a flag
saying it was not.

**The counter-pressure this ADR exists to resist** is symmetry. Two directories,
one with reconstructions and one without, looks like an oversight, and
"consistency" is a persuasive argument for closing the difference. The
difference is the point: one records what happened, the other records what was
seen, and only the first survives its own absence.

**Reconstructions are a debt, not a mechanism.** There are two, both from the
same cause — a control shipping in the same change as the action it was meant to
observe. A third would mean the pattern has become the process, and the fix is
to ship the recorder before the capability it records, not to keep
reconstructing afterwards.

## Alternatives considered

**Add `provenance` to `fw-violation/v2` and reconstruct the finding.** Rejected
above. It would have made the reorder look identical to the deletion at both
ends, at the cost of asserting a detection nobody performed.

**Leave the crashed deletion unrecorded.** Rejected on 2026-08-16. An
irreversible act with no record is the failure `delete-scm-object.yml` exists to
prevent, and a bug in the writer does not make the act undocumented-by-design.

**Record nothing after the fact, ever.** Consistent and simple, and it throws
away recoverable truth: the object id, tags and timestamp of the lost deletion
were all still in the run log at the time. Losing them to a rule would have been
a choice, not a limitation.
