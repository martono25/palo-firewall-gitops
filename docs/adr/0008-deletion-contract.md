# ADR-0008 — What removing an intent means, per kind

- **Status:** Accepted (2026-08-06) · **Amended 2026-08-09** (evidence for removals)
- **Date:** 2026-08-06
- **Deciders:** Martono, Claude

## Context

Deleting an intent was, until v1.30.0, **invisible to the entire risk pipeline**.
`classify` reads the intent tree, and a deleted intent is simply absent — so
nothing tiered it, the gate never saw it, and no evidence was produced. Terraform
was the only stage that knew, and only at plan time.

Removal also worked, in the sense that objects disappeared. But it worked as a
BEHAVIOUR nobody had written down: no test asserted it, no document said what
removal was supposed to mean, and the per-kind differences — which are large —
were unknown until they were measured.

They have now been measured on the live pilot. This ADR states the contract those
measurements support, so the next person relies on something written rather than
rediscovering it against a production firewall.

## What was measured

| kind | on removal | evidence |
|---|---|---|
| **ZoneRequest** | SCM **refuses** while a rule references it: `409 NON_ZERO_REFS`, naming the exact referencing path. Unreferenced, it deletes cleanly and the interface survives **addressed but unzoned** — PAN-OS drops traffic on an unzoned interface. | tested end to end, 2026-08-05 |
| **RouteRequest** | **Nothing refuses it** at any layer. The `prod-edge` override is destroyed and the folder reverts to the inherited `ngfw-shared` router: VRF interface membership survives, routes empty. On the device the default route disappears ~40s after the push reports success; connected routes remain. Intra-subnet traffic keeps working, **everything off-subnet is black-holed**. | tested end to end, 2026-08-06 |
| **InterfaceRequest** | A device-scope override reverts to the **inherited object, which carries no addressing** — the firewall loses the IP on that interface. | `spike/device-override-probe`, and again unintentionally when the 2026-08-05 re-onboard wiped all three overrides |
| **AccessRequest** | Not tested on hardware. Same mechanism as a zone, and uniquely it carries `gitops:` tags, so an orphaned rule is **detectable** by drift where zones, routers and interfaces are not. | mechanism inferred, orphan detection built (v1.3.0) |

The asymmetry is the point. A route deletion is an outage with **no error and no
backstop**; a zone deletion is refused outright while anything references it. Two
kinds, opposite failure modes, and nothing in the platform distinguished them.

## Decision

**1. A removal is a first-class change.** It is classified and gated like any
other (v1.30.0, `classify --baseline`). This ratifies what was built rather than
leaving it as an implementation detail.

**2. Removal tiers are per kind, and are NOT the mirror of creation.**

| removal | tier | reasoning |
|---|---|---|
| `allow` rule | LOW | withdraws access — can break what depended on it, opens nothing |
| `deny` rule | HIGH | traffic it blocked may now match a permissive rule below: a removal that INCREASES access |
| route | HIGH | silent black-hole, no backstop |
| zone | HIGH | interfaces left unzoned; traffic dropped |
| interface | HIGH | addressing lost |
| **unknown kind** | **CRITICAL** | see (4) |

**3. The platform guarantees a VISIBLE deletion, not a SAFE one.** This is the
honest statement and it is deliberate. The only thing that ever refused a
deletion was SCM's reference check, which exists for referenced objects and
nothing else — it is incidental protection, not a designed one. Claiming safety
would be claiming a control that does not exist, which is the failure this
project removed from `expires` and from `DESIGN.md`. What is guaranteed is that a
removal is tiered, reported and reviewable **before** it applies.

**4. A new kind must have its removal behaviour MEASURED before it ships.** Until
it does, its removals are CRITICAL — `classify_removal` defaults there rather
than to LOW. A permissive default is how the whole class went unassessed in the
first place. This mirrors the existing rule that a kind must be checked for tag
support before drift coverage is promised for it.

## Consequences

**`NatRequest` removals are CRITICAL until measured.** That is the rule working,
not an oversight to fix by special-casing it.

**Deleting the last intent of a kind removes its tfvars file** (v1.18.0), so the
object is genuinely destroyed rather than silently re-asserted from a stale file.
That fix is load-bearing for this contract.

**Removals produce an evidence bundle** — ~~open~~ **decided and built, v1.37.0.**
See the amendment below.

**Two gaps are known and not closed by this ADR:**

* `device-sync` cannot see an applied-but-unpushed change, because Terraform
  writes to SCM's candidate and only a push creates a version to compare. So the
  window between "destroyed in SCM" and "delivered to the device" is not
  monitored.
* `AccessRequest` removal is untested on hardware. It is the lowest-risk of the
  four and the only one with orphan detection, which is why it is last — but
  "inferred" is recorded here as inferred, not as measured.

## Alternatives considered

**Refuse deletions above a tier outright.** Rejected: it is the gate's job, and
the gate is already configurable per run (`--gate`, `max_auto_tier`). A second,
harder block would either duplicate it or contradict it.

**Require a two-step deletion (disable, then remove).** Attractive for rules,
where `disabled: true` is a real PAN-OS field and would make a removal reversible
in seconds. Rejected for now because it does not generalise: there is no
"disabled" state for a route, a zone or an interface override, so it would give
one kind a safety property the others cannot have — and an inconsistent contract
is harder to reason about than a uniform one. Worth revisiting if rule deletions
become frequent.

**Model deletion as an explicit intent (`kind: Revocation`).** Rejected: Git
already records removal precisely, and an explicit revocation object would need
its own lifecycle — including how to delete *it*.


---

## Amendment — 2026-08-09: evidence for a removal

The original said bundles for removals were "open, and it needs a decision about
what *applied* means for a deletion before it can be built rather than guessed."
Three decisions, taken rather than guessed.

### A1 — A removal TOMBSTONES the object's own record, in place

`evidence/<scope>/<REQ-id>.json` is overwritten with `status: removed`.

The alternative — a separate `evidence/removed/` tree — was rejected because it
forks a request into two files, and a later re-created `REQ-2026-0727` would
collide with its own tombstone. Deleting the bundle outright was rejected
outright: an audit trail that disappears at the moment someone goes looking for
it is the `expires` failure and the artifact-TTL failure a third time.

One file per request is what makes `git log evidence/<scope>/<REQ>.json` that
request's whole life — created, changed, removed. The removed object is
**embedded from the baseline tree**, so the record still says WHAT went; reading
git history is not required to answer that.

### A2 — `removed` means destroyed in SCM AND pushed

The same bar `applied` meets. A destroy whose push is refused is `failed`, not
`removed`.

This is not theoretical: the route-deletion test on 2026-08-06 destroyed the
logical router in SCM and had the push refused, leaving SCM saying "no default
route" while the device still forwarded on one. A record claiming `removed` on a
successful `terraform destroy` alone would have been FALSE for that window.

A separate delivered-to-device status was rejected — it invents a distinction
creates do not have, and delivery is `device-sync`'s question, not the bundle's.

### A3 — A removal carries its OWN change ticket, via a `Removes:` trailer

    Removes: REQ-2026-0727 (JIRA-31555)

**The problem.** A MODIFIED intent proves its own authorisation: since v1.33.0
`stale_ticket_problems` requires `metadata.ticket` to move with the spec. A
REMOVAL cannot, because the fix is deleting the file — there is nowhere left to
write the new ticket. Left alone, the record for an August deletion would carry
`ticket: JIRA-20727, requested: 2026-07-26` — the ticket that authorised
**creating** the object. That is the same false statement in a CM-3 artifact
that v1.33.0 removed, arriving through deletion instead of modification.

**Why a trailer.** It puts the authorisation where the change lives, and needs no
new object lifecycle. A tombstone INTENT (`state: removed` plus a fresh ticket)
was rejected for the same reason `kind: Revocation` was rejected in the original
ADR: it needs its own answer to "when does *that* get deleted?".

**Where it is read.** Whatever text lands on `main`. With squash merges that is
the PR title + body, so `pr-validate` checks the PR body — deliberately NOT the
individual commit messages, which would pass a PR whose trailer never reaches
main — and `apply` re-reads it from the merged commit.

**Fail-closed.** A removal with no trailer is REJECTED (exit 2), on the PR, while
the author is still there to fix it. A trailer naming a different request does
not authorise this one.

### What this does not change

The stance in decision (3) stands: **the platform guarantees a VISIBLE deletion,
not a SAFE one.** A removal is now also RECORDED. Recorded is not safe, and this
amendment should not be read as making the earlier claim stronger than it was.
