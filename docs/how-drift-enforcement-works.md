# How drift enforcement works

*Explanation — why the system is shaped this way. For what to DO about a finding,
see [operator-runbook.md](operator-runbook.md); for the commands, see
[cli-reference.md](cli-reference.md).*

Every GitOps platform claims Git is the source of truth. Most of them mean it as
a convention: the config in Git is what *should* be running, and if somebody
changes the firewall by hand, that is a conversation rather than a mechanism.

This one enforces it. Config no request authorised is deleted. Config Git
declares is restored. Both happen unattended, nightly, and both leave a record.

## The problem

A convention that nothing enforces decays in one specific way, and it is worth
being precise about it.

Someone opens a path by hand at 2am to restore a service. It works. The incident
closes. The rule stays, because removing it is somebody's Tuesday problem and
nobody is quite sure what depends on it now. Six months later the firewall
carries a dozen rules nobody can account for, an audit asks who approved them,
and the honest answer is that the platform never knew they existed.

The failure is not that the emergency change happened. It is that nothing
*noticed*, so the temporary thing became permanent by default.

That is the easy case. The harder ones are quieter:

- Somebody edits a managed rule's destination in the console. The rule keeps its
  name, its tags, its request id. Every provenance check passes. It now passes
  traffic no request authorised, and it looks identical to the original.
- Somebody drags a permissive rule above a restrictive one. **No rule was added,
  removed, or edited.** Every one of them is authorised. What changed is which
  rule matches first, which is the policy.
- Somebody switches a rule off. It still exists, still tagged, still named after
  its request. It matches nothing at all.

A platform that only asks *"who created this?"* sees none of the last three.

## The approach

Two questions, asked separately, an hour apart.

```
  09:00 SGT   drift-detect        WHAT IS DIFFERENT?     (read-only)
                 |                 records every finding, fails the run
                 |
                 |  <-- one hour. A finding can still be fixed by hand here.
                 v
  10:00 SGT   remediate           WHAT SHOULD BE DONE?   (writes)
                                   deletes, then restores
```

Detection never writes. Remediation never guesses — it re-reads SCM rather than
acting on the records detection wrote, because a record an hour old describes a
world that may have moved.

### Six engines across four steps

The nightly job runs four steps; three of them carry more than one engine,
because a folder read that has already happened can answer several questions.

| Engine | Answers | Covers | Step |
|---|---|---|---|
| tag | who owns this rule? | rules added, removed, or forged | rule drift |
| content | does this rule still MATCH what Git declares? | every field of a managed rule | rule drift |
| order | are our rules in the sequence they were deployed? | the rulebase | rule drift |
| state | is this object what Git declared? | zones, routes, interfaces — kinds that **cannot carry tags**, so provenance is unreadable and only presence and content can be compared | state drift |
| provenance | does anything account for this address or service? | addresses, services | object drift |
| `terraform plan` | has managed state diverged? | the backstop, and the only engine that predates v3.0.0 for rules | plan drift |

They exist separately because they answer genuinely different questions. The tag
engine reads provenance from tags; it can tell an unauthorised rule from an
abandoned one, but it cannot tell whether an authorised rule still does what it
was approved to do. The content comparison can, and it also sees what
`terraform plan` structurally cannot: the SCM provider treats `application` as
computed so it never fights `enrich` over App-ID, which means an application
edited in the console produces no plan diff at all.

Every detector **records its verdict and passes**. A single gate at the end turns
the collected verdicts into one failure. That shape matters: when each detector
exited on its own first finding, it skipped every check below it, so the night
with the most drift reported the least of it.

### Six classes, because the remedy differs

```
modified    an authorised rule's live config differs from Git
malformed   carries gitops:managed but traces to no request of its own
reordered   a managed rule is not in its deployed position
missing     a rule Git declares is absent from SCM
unmanaged   nothing accounts for it at all
orphaned    authorised once, no longer declared
```

`modified` ranks first in severity, which surprises people. It is the quietest:
the rule is authorised, correctly named and correctly tagged, so nothing looks
wrong on inspection while the effective policy has changed.

## One rule decides every remedy

Not six judgements. One question:

> **Does Git declare this object?**

- **Not declared** — nothing says it should exist. **Delete it.** That covers
  `unmanaged`, `orphaned`, and the `malformed` copy whose name Git declares
  nowhere.
- **Declared** — Git says it should exist and something about it is wrong.
  **Restore it.** That covers `modified`, `missing`, `reordered`, and the
  `malformed` original whose tags were damaged.

The first version of this deleted `unmanaged` only, on the reasoning that the
other classes "need judgement". They do not. Every drift class is unauthorised
*state*; what differs is the correct *action*, and source-of-truth answers that
without anyone exercising taste.

**The guard keys on the object's own NAME, never on a tag it carries.** Keying on
`gitops:req` looks like belt-and-braces and does the opposite: a console copy
inherits the original's tag, so a tag-based guard shields the forgery precisely
because it is wearing the original's label. A managed rule is named after its
request, and a name is the one claim a copy cannot inherit.

## Both halves leave a record

Detection files a `fw-violation/v1` record per finding. Remediation files a
`fw-manual-action/v2` naming what it did and the finding it answers.

Records are **findings, not events**: one file per violation identity, so the
same violation seen on ten nights is one record with `first_seen`, not ten. A
record is **resolved, never deleted** — "this was open for six days in August" is
what a follow-up process needs afterwards.

Two properties are worth testing, because both are places this could quietly
lie:

- **A scope that was not read cannot resolve its findings.** If a folder's SCM
  read fails, its open violations stay open. An outage in the checker must not
  read as a clean bill of health.
- **A detector that saw only objects cannot close a rule finding.** Several
  detectors run over the same folder, each knowing one kind. Resolution keyed on
  scope alone let the object checker close every rule finding in a folder it had
  just read — a finding that closes because a *different* detector ran is worse
  than one never raised.

## Trade-offs

**An emergency change now has a deadline.** A hand-made fix survives until
10:00 SGT and no longer. This is the cost of enforcement, accepted deliberately:
it is a timing constraint on the operator, not a reason to keep deletion manual.
Raise and apply an AccessRequest before the run. Note that doing so creates a
*new* rule under its request id — the hand-made one is still unmanaged and is
still deleted, which is the intended end state.

**Restoration runs without an approval gate.** Every rule a restore re-asserts
was approved when its intent was merged, and requiring a second approval to put
back what was already approved would leave unauthorised config live while a
request sat in a queue. The gate stays for *new* policy. The restore refuses to
run when `main` holds intent that has never been applied, so it cannot become a
way around that gate.

**Undeclared fields are not compared.** A field the intent leaves unset can be
changed in the console without this noticing. The remedy is to declare it — at
which point it is compared like anything else. The alternative is worse:
asserting a value this platform never writes produces drift no remediation can
fix, which is a permanently red job, which is a detector somebody switches off.

**Objects inside a hand-made snippet read as SCM-provided.** Provenance comes
from SCM's `snippet` marker, and snippets are a construct users can also create.
Closing this needs snippet-level management, which this repository does not have.
Tracked in [TODOS.md](../TODOS.md).

## Alternatives considered

**Adopt unmanaged config instead of deleting it.** Rejected in
[ADR-0011](adr/0011-unmanaged-drift-is-deleted.md). Adoption does not exist as an
operation here: a managed rule is named after its request id, so an intent
describing a hand-made rule compiles to a *different* rule. Applying it creates a
second rule and leaves the original untouched — still unmanaged, now duplicated.

**An allowlist of permitted pre-existing objects.** Designed, then rejected: a
baseline a user can edit is a way to launder an unauthorised object by adding its
name to it. A bypass wearing the costume of a control. Provenance is read live
from SCM instead, so there is nothing stored and nothing to amend.

**Reconstructing a missing violation record by hand.** Rejected in
[ADR-0012](adr/0012-what-may-be-reconstructed.md). An action record asserts "this
change was made", which is true regardless of who was watching. A violation
record asserts "this was OBSERVED, by this run, at this time" — creating one
afterwards claims a detection nobody performed.

## Related

- [operator-runbook.md](operator-runbook.md) — what to do when drift fires
- [cli-reference.md](cli-reference.md) — `remediate`, `objects drift`, `drift`
- [assessor-guide.md](assessor-guide.md) — what the records prove
- [ADR-0011](adr/0011-unmanaged-drift-is-deleted.md) — unmanaged drift is deleted
- [ADR-0012](adr/0012-what-may-be-reconstructed.md) — reconstruct actions, never detections
