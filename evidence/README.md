# evidence — NIST-mapped change evidence (Git-resident)

One JSON record per change, written by `fwgitops.evidence`, committed to Git
(Git = SSoT). Layout: `evidence/<scope>/<REQ-id>.json`, where `<scope>` is an
SCM folder (`prod-edge/`) or a single firewall (`device-<serial>/`) — the same
split the Terraform roots use, because a firewall is addressed `device=`, never
`folder=`.

**Every kind, since v1.36.0 (schema `fw-evidence/v2`).** Bundles used to be
rule-shaped, so only `AccessRequest` produced one: ten intents in this repo
produced five records, and changing a default route, an interface address or a
zone left no audit trail at all. The bundle is now assembled from the kind
registry, and a `kind` field says which one each record describes.

An assessor or incident responder can reconstruct **what changed, who authorised
it, why, and what the system checked** from the bundle alone — no CI logs, no
ticket archaeology, no SCM UI.

## Sections

| Section | Contains |
|---|---|
| `kind` | which intent kind this record describes |
| `removal` | on a `removed` record: the ticket authorising the REMOVAL, and the commit |
| `request` | requester, ticket, justification, requested date, intent file + sha256 — **paperwork only** |
| `compiled` | scope, the compiled object + its sha256, compiler version, tfvars file + sha256 |
| `risk` | tier, classifier + threshold versions, checks fired (Phase 2) |
| `approval` | gate, approvers (`login` + `via`), PR, merge commit |
| `controls_not_evidenced` | controls this record deliberately does NOT claim, and why |
| `apply` | plan sha256, CI run URL |
| `push` | folder-scoped push result + job id |
| `controls` | NIST control coverage for this record |

## Properties

- **Hashes, not copies** — intent/tfvars/plan referenced by sha256: small but
  tamper-evident. Git supplies history and timestamps.
- **Versioned** — compiler/classifier/threshold versions recorded, so a past
  decision stays reproducible after the rules change.
- **A removal is a record, not a gap** — deleting an intent overwrites its bundle
  with a `status: removed` **tombstone** carrying the object as last applied, so
  `git log evidence/<scope>/<REQ>.json` is that request's whole life. `removed`
  means destroyed in SCM *and* pushed; a refused push is `failed`. The removal's
  own ticket comes from a `Removes: <REQ-id> (TICKET)` trailer in the text that
  lands on main — the intent's `metadata.ticket` authorised *creating* the
  object, and the file it lived in is gone. See ADR-0008's 2026-08-09 amendment.
- **Failures are evidence too** — `status` is `applied` / `rejected` / `failed` /
  `removed`;
  a fail-closed push that refused to commit unexpected drift is exactly what you
  want on record. Failure statuses require a `failure_reason`; `removed` requires
  a `RemovalContext`.
- **Deterministic** — byte-stable JSON, so re-generating never churns Git.
- **One commit per CHANGE, not per apply** — a record whose change is unchanged
  is left exactly as committed. Every apply regenerates every bundle and
  `generated_at` always moves, so without this every apply committed every
  record, each stamped with that run's `run_url` and `merge_commit` — a request
  nobody touched claiming to have been applied by a run that applied something
  else. Identity is `schema · kind · status · intent_sha256 · object_sha256`.
- **`object_sha256` is per REQUEST; `tfvars_sha256` is per FILE** — several
  requests share one tfvars file (every rule in a folder writes
  `rules.auto.tfvars.json`; every route for a VRF aggregates into one router),
  so the file hash moves when a neighbour changes and says nothing about this
  request.
- **No secrets** — asserted by test.
- **Paperwork and behaviour are separated** — `request` carries the change
  record; anything describing what the firewall will do lives under `compiled`,
  derived from the spec so it cannot silently disagree with it. Mixing the two
  is what let an edited rule keep the ticket that authorised its previous
  version (see `removal.stale_ticket_problems`).
- **The object is serialised whole** — the v1 bundle listed rule fields by hand
  and the list fell behind the compiler twice. A field added to a compiled type
  now reaches the audit record without a second edit.

## Controls (NIST SP 800-53 Rev.5)

Unconditional, because they hold from the record's own contents: `AC-4`
information flow enforcement (the rule *is* the flow control) · `CM-3`
configuration change control · `AU-2` / `AU-12` audit events + record
generation · `SC-7` boundary protection.

**Conditional — a listed control is a claim it was OPERATING:**

- `CM-5` access restrictions for change — claimed **only when an approver is
  named**. It was unconditional until v1.38.0 while `approvers` was hard-coded
  empty and nothing ever passed one, so every bundle claimed "who may approve vs
  who did" and answered nobody. When it is absent the control is omitted *and*
  the omission is listed in `controls_not_evidenced`, because a silently shorter
  list reads as an older schema rather than a gap.
- `AC-5` separation of duties — CRITICAL-tier dual-control changes.

Approvers record **which restriction was exercised**, not just a name:
`pull_request_review` (reviewed the proposed change) and `deployment_gate`
(released this deployment) are different acts, and one person doing both is a
finding rather than a detail.

The point is evidence that the control **was operating** — the fired checks plus
the classifier version prove a specific decision by a specific ruleset at a
specific time — not merely documentation that a process exists.
