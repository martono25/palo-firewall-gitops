# evidence — NIST-mapped change evidence (Git-resident)

One JSON record per change, written by `fwgitops.evidence`, committed to Git
(Git = SSoT). Layout: `evidence/<folder>/<REQ-id>.json`.

An assessor or incident responder can reconstruct **what changed, who authorised
it, why, and what the system checked** from the bundle alone — no CI logs, no
ticket archaeology, no SCM UI.

## Sections

| Section | Contains |
|---|---|
| `request` | requester, ticket, justification, expiry, intent file + sha256 |
| `compiled` | folder, objects, rule, tags, compiler version, tfvars sha256 |
| `risk` | tier, classifier + threshold versions, checks fired (Phase 2) |
| `approval` | gate, approvers, PR, merge commit |
| `apply` | plan sha256, CI run URL |
| `push` | folder-scoped push result + job id |
| `controls` | NIST control coverage for this record |

## Properties

- **Hashes, not copies** — intent/tfvars/plan referenced by sha256: small but
  tamper-evident. Git supplies history and timestamps.
- **Versioned** — compiler/classifier/threshold versions recorded, so a past
  decision stays reproducible after the rules change.
- **Failures are evidence too** — `status` is `applied` / `rejected` / `failed`;
  a fail-closed push that refused to commit unexpected drift is exactly what you
  want on record. Failure statuses require a `failure_reason`.
- **Deterministic** — byte-stable JSON, so re-generating never churns Git.
- **No secrets** — asserted by test.

## Controls (NIST SP 800-53 Rev.5)

`AC-4` information flow enforcement (the rule *is* the flow control) · `CM-3`
configuration change control · `CM-5` access restrictions for change · `AU-2` /
`AU-12` audit events + record generation · `SC-7` boundary protection.
`AC-5` (separation of duties) is added automatically for CRITICAL-tier
dual-control changes.

The point is evidence that the control **was operating** — the fired checks plus
the classifier version prove a specific decision by a specific ruleset at a
specific time — not merely documentation that a process exists.
