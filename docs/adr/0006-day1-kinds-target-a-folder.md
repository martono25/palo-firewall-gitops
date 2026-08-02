# ADR-0006 — Day-1 kinds name their `folder:`; `AccessRequest` keeps `environment:`

- **Status:** Accepted — **built** (v1.11.0), **corrected** (v1.11.1 — see the
  correction note; the device-folder premise below was wrong)
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

1. **`environment` resolves 1:1, so a second folder cannot be addressed without
   editing platform config.** Every new target meant a new entry in
   `catalog/environments.yaml`.

   *(This point originally read "…cannot name a device folder at all," citing
   per-device folders under `prod-edge`. That was wrong — see the correction
   note. The decision below does not depend on it.)*
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
- Targeting a folder is an intent PR, reviewed like any other change — not an
  edit to platform config.
- The sandbox folder `GitOps` is addressable directly, so Day-1 kinds can be
  exercised end to end without a platform-config edit.
- The blast-radius footgun needs a deliberate `targetable: true` in a reviewed
  catalog PR to unlock.

**Negative / accepted**
- Two addressing forms in one intent model. Justified by different authors and
  different purposes, but it is more surface to document and explain.
- `catalog/folders.yaml` must be kept in step with SCM by hand. Verifying it
  against live SCM belongs in the drift work — including the `type` check
  described in the correction note, which would have caught the mistake below.

**Follow-on**
- `catalog/routers.yaml` is keyed by folder, so targeting a device folder needs a
  router entry for it — correctly fail-closed today, and part of the Day-1 apply.
- A device folder needs its own Terraform root before anything can be applied
  there; the missing-root guard already refuses to emit without one.

## Correction (2026-08-02, v1.11.1)

**The device-folder premise in this ADR was wrong, and v1.11.0 shipped it.**

`GET /config/setup/v1/folders` returns two kinds of entry, distinguished by
`type`:

```
{"name": "prod-edge",       "type": "container"}                      <- a folder
{"name": "007955000894453", "type": "on-prem",
 "serial_number": "007955000894453", "model": "PA-VM"}                <- a DEVICE
```

Reading that listing, I took the two `on-prem` entries parented to `prod-edge`
for per-device folders, wrote them into `catalog/folders.yaml` as children of
`prod-edge`, and marked them `targetable: true`. They are not folders:

* `GET /config/network/v1/zones?folder=007955000894453` → **400 API_I00013,
  "Folder 007955000894453 doesn't exist"**
* the same serial as `device=007955000894453` works, returning `ethernet1/3`
  and `ethernet1/4`
* pan.dev documents `folder`, `snippet` and `device` as three separate query
  parameters, and the Terraform provider states "exactly one of `device`,
  `folder`, `snippet`" on every resource

An intent naming a serial would have **compiled clean and failed only at apply**.

Two knock-on corrections:

* v1.11.0's claim to have *fixed* an understated blast radius was itself the
  error. `prod-edge: children: []` was right — it has no child containers. Its
  two firewalls are devices attached to it, and a change to `prod-edge` reaching
  both of them is that folder's purpose, not a hidden fan-out. The catalog and
  its test are back to `children: []`.
* **Targeting a single firewall remains unsolved.** It needs a `device:` scope,
  which the resources support (`scm_zone`, `scm_ethernet_interface` and
  `scm_logical_router` all take `folder` / `snippet` / `device`) but this
  platform does not implement. That is a design decision, not a catalog entry.

The rest of this ADR stands: `folder:` on the Day-1 kinds, `environment:` on
`AccessRequest`, exactly one of the two, and targetability enforced at compile
time. Only the claim that a device is a folder was wrong.

**Process note.** Three independent sources agreed once I checked — the live API,
the provider schema, and the docs. I checked none of them before writing the
premise into an ADR, a catalog and a test; the folder listing *looked*
unambiguous. The standing rule is to verify against pan.dev before asserting how
SCM behaves, and it applies to confirming a belief, not only to diagnosing a
failure.
