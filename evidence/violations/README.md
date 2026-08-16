# Violation records — `fw-violation/v1`

One file per finding, keyed on the object it concerns (scope + kind + name), so
the same violation detected on ten nights is one record with `first_seen` and
`last_seen` rather than ten. A record is **resolved, never deleted** — "this was
open for six days in August" is what a follow-up process needs afterwards.

See `docs/adr/0011-unmanaged-drift-is-deleted.md` for what happens to a finding,
and `docs/adr/0012-what-may-be-reconstructed.md` for why a record here can never
be written by hand.

## Records may not be deleted just because they are stale

Tried on 2026-08-16 and reverted the same hour. Every record present described a
test fixture, so clearing them looked like tidying — and it broke the audit
chain. `evidence/manual-actions/` links each remediation to the finding that
justified it by `violation_id`, so deleting the finding leaves the remediation
pointing at nothing. A dangling link is worse than no link: it reads as
provenance while providing none, and a test refuses it.

Five records WERE removed: `reordered` findings for rules nobody had touched,
produced by a defect since fixed. Deleting one rule shifted every index after
it, so the order check blamed the survivors. Nothing referenced them and they
described no event.

The distinction is not "is this record old" but **"did this record ever describe
something that happened"**. Findings about test fixtures did. Findings produced
by a bug did not.
