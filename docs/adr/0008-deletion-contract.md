# ADR-0008 — What removing an intent means, per kind

- **Status:** Accepted (2026-08-06)
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

**Removals still produce no evidence bundle.** Bundles are built per request, and
a deletion has no request in the current tree — though the baseline tree does
hold it, so one is buildable. Open, and it needs a decision about what "applied"
means for a deletion before it can be built rather than guessed.

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
