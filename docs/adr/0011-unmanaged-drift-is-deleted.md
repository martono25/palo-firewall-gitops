# ADR-0011 — Unmanaged drift is DELETED, never adopted

**Status:** accepted (2026-08-16)

## Context

A rule that appears in SCM without this platform's provenance tags is
`unmanaged` drift. The nightly job now detects it (ADR-0009's tag engine, wired
2026-08-15) and fails the run while it exists.

The question that follows is what to DO with it, and the runbook answered it
wrongly for a day: it said to "adopt" the object by writing an intent that
describes it. **That operation does not exist.** A managed rule is named after
its request id — `security_rules[ch.rule.name]` is the `for_each` key and
`name = ar.metadata.id` — so an intent describing `Testing-unmmanaged` compiles
to a DIFFERENT rule. Applying it creates a second rule and leaves the original
untouched: still unmanaged, still drifting, now duplicated.

Genuine adoption would require `terraform import` plus a rename, because the
live name can never be a managed name. That preserves the object's UUID and
rulebase position at the cost of write access to production Terraform state and
a failure mode — importing the wrong object — whose next apply modifies or
destroys something nobody intended. Neither property it preserves is used
anywhere: nothing references a rule by UUID, and rule ORDER is not tracked at
all (see the assessor guide).

## Decision

**Unmanaged drift is deleted. It is never adopted, and `terraform import` is not
built for this purpose.**

An emergency change made directly in SCM is a legitimate operational act — a
firewall exists to be changed when something is on fire. It is not a legitimate
permanent state. Once the emergency is over the rule is raised as a normal
`AccessRequest`, and the object made by hand is removed.

**The order matters, and it is not the obvious one:**

1. **Emergency fix in SCM.** Traffic flows. Drift goes red at the next run,
   which is correct and is the reminder that step 2 is outstanding.
2. **Raise the `AccessRequest`** — PR, review, tier gate, apply. The permanent
   rule now exists ALONGSIDE the emergency one. Both are live; the traffic is
   covered twice, which is harmless.
3. **Delete the hand-made object** (`delete-scm-object.yml`), which records the
   removal. Drift goes green.

Deleting first would open a coverage gap between the removal and the apply — at
exactly the moment someone is under pressure and least able to absorb one.

## Consequences

**Good.** There is one answer to "what do I do about this", and it does not
depend on judging whether the change was legitimate — that judgement moves to
step 2, where it belongs, with a ticket and an approver. The platform never
takes ownership of an object it did not create, so the ownership proof in
`objectsweep.is_ours` keeps meaning what it says. And no tooling is built that
can write to production Terraform state.

**Costs.** The permanent rule is a NEW object: new UUID, and it lands at the
bottom of the pre-rulebase rather than wherever the emergency rule sat. If order
mattered to the emergency fix, the `AccessRequest` must say so explicitly with
`position:` — the platform will not infer it, and nothing will warn you.

**What enforces it.** Nothing blocks an emergency change; the pressure is that
drift stays RED until step 3 is done. That is deliberate — a control that
prevented the 3am fix would be routed around, and one that stayed silent
afterwards would leave the estate permanently diverged.

**Not foreclosed.** Onboarding an EXISTING estate — a folder of hand-built rules
brought under management without recreating them — is a different problem, and
`terraform import` is the right tool for it. It needs reverse-compilation of live
config into intent and a name-reconciliation strategy, and it deserves its own
ADR. This decision is only about drift remediation.
