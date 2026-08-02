# ADR-0006 — Day-1 kinds name their `folder:`; `AccessRequest` keeps `environment:`

- **Status:** Accepted — **built** (v1.11.0)
- **Date:** 2026-08-02
- **Deciders:** Martono, Claude

## Context

Every kind addressed its target the same way: `environment: prod`, which
`catalog/environments.yaml` maps 1:1 to an SCM folder. That indirection is the
point of the Day-2 request surface — app teams should never need to know SCM
topology, and renaming a folder must not invalidate every open request.

Extending it to the Day-1 kinds was never a decision, just inheritance. It broke
the moment we tried to apply the chain to real hardware.

### What forced the question

Choosing where to prove the Day-1 chain, the only way to express "this specific
firewall" was to **add an entry to `catalog/environments.yaml`** — turning a
one-off targeted change into an edit of platform config. The venue question had
no home in the model.

Three things were wrong:

1. **`environment` resolves 1:1, so it cannot name a device folder at all.** SCM
   parents a folder under `prod-edge` for each onboarded device
   (`007955000894453`, `007955000893662`). That is the tightest scope which still
   reaches real hardware, and the model could not express it.
2. **The author is different.** An `InterfaceRequest` or `RouteRequest` is
   written by a network engineer, for whom the folder IS the intent. Abstracting
   it away serves nobody.
3. **A new target required a platform-config PR**, so the catalog would accrete
   entries that exist only to name somewhere a change once had to land.

ADR-0001 already holds the principle: kinds **declare capability rather than fake
uniformity**. One addressing model across app-language and infrastructure kinds
is exactly that faked uniformity.

## Decision

**Per-kind addressing.**

| Kind | Targets | Author |
|---|---|---|
| `AccessRequest` | `environment:` | app teams (broad) |
| `InterfaceRequest` / `ZoneRequest` / `RouteRequest` | `folder:` | network engineers |

The Day-1 kinds take **exactly one** of `folder:` / `environment:` — the
`environment` form is kept because it is the right vocabulary when a change
really is per-environment, and dropping it would churn every existing intent.

### `folder:` is guarded by the catalog, not by the classifier

An unguarded folder field lets a requester name `ngfw-shared` and reach every
device at once. `catalog/folders.yaml` gains `targetable: true|false`, and an
intent naming an unknown or non-targetable folder is **rejected at compile
time**.

Rejected, not tiered up. The classifier's `folder_with_children` check still
fires HIGH — but **HIGH is approvable**, and a write to a shared parent should
not be one rubber-stamp away. Two different jobs: the classifier prices risk, the
catalog decides what is addressable at all.

Fail closed throughout: an unknown folder is not targetable, and a **missing**
catalog makes `folder:` unusable rather than unchecked.

Targetability deliberately does **not** apply to the `environment:` path.
`catalog/environments.yaml` is reviewed platform config; the threat model here is
the field a requester writes.

### `AccessRequest` rejects `folder:` rather than ignoring it

`folder:` was previously an unknown key there, silently ignored — so an
`AccessRequest` that copied it from a Day-1 example landed in whatever
`environment` resolved to while its author believed otherwise. Now that `folder:`
is meaningful vocabulary elsewhere in the model, that is a silently wrong target:
the exact failure class this platform exists to prevent. It is rejected with a
message naming `environment:`.

## Consequences

**Positive**
- The device folder is addressable, so the Day-1 chain can be proven on one
  firewall without touching the other.
- Targeting a folder is an intent PR, reviewed like any other change — not an
  edit to platform config.
- The blast-radius footgun needs a deliberate `targetable: true` in a reviewed
  catalog PR to unlock.

**Negative / accepted**
- Two addressing forms in one intent model. Justified by different authors and
  different purposes, but it is more surface to document and explain.
- `catalog/folders.yaml` must track device onboarding. It was **already stale** —
  it declared `prod-edge: children: []` while SCM had two device folders under
  it, so the classifier scored changes to the production folder as reaching no
  descendants when they in fact reach both firewalls. Understating blast radius
  on the production folder is the worst direction to be wrong in. Verifying the
  hierarchy against live SCM belongs in the drift work.

**Follow-on**
- `catalog/routers.yaml` is keyed by folder, so targeting a device folder needs a
  router entry for it — correctly fail-closed today, and part of the Day-1 apply.
- A device folder needs its own Terraform root before anything can be applied
  there; the missing-root guard already refuses to emit without one.
