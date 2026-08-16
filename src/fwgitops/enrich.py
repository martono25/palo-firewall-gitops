"""SCM enrich step — applies rule ORDERING, which Terraform cannot express.

NARROWED in v1.16.0. This module used to write `application`, `profile_setting`,
`log_setting`, `source_user`, `category`, `negate_*`, `log_start` and
`description` as well, because the provider (v1.0.11 / 1.0.12-beta.3) accepted
those and silently discarded them — the ADR-0003 finding.

Provider **1.0.12-beta.4 writes them**, and the module wires them, so those
fields now have exactly one writer: Terraform. Doing it here too would be worse
than wasteful — a redundant write silently repairs a field Terraform failed to
set, so a regression in the module would never surface. See the ADR-0003
addendum.

WHAT REMAINS, and why it does not move to Terraform AS THIS MODULE IS SHAPED.
An anchored move needs the anchor's UUID:

    target_rule = scm_security_rule.this[<key>].id

which is a self-reference inside a single `for_each` block, and Terraform
rejects it:

    Error: Cycle: module.security_folder.scm_security_rule.this["REQ-..."], ...

NOT A TERRAFORM LIMITATION — corrected 2026-08-15. The provider documents this
exact pattern (Example 4 on `scm_security_rule`), referencing a DIFFERENT
resource block: `target_rule = scm_security_rule.standard_web_access.id`. It
works. Measured on Terraform v1.15.8: two resources, anchored -> unanchored,
plans clean; one `for_each` referencing its own instances cycles even when
nothing is genuinely circular, because those instances are one graph node.

So the constraint is OUR shape — every rule in a single `for_each` — not
Terraform's capability. A split into anchored and unanchored resources would
work for depth-1 anchoring, and cycles again for chains (also measured). See
TODOS for the table.

`top` / `bottom` need no anchor and ARE honoured through `relative_position`;
only before/after ordering lands here. The SCM move endpoint works when called
with the anchor's UUID, which is what this module does.

    terraform apply ──▶ rules created WITH their fields
         enrich_folder() ──▶ ordering (before/after moves)
             push ──▶ commit (both together, one atomic change)

The seam is post-apply / pre-push: the rules already exist, and the moves land in
the SAME candidate as Terraform's writes, so one admin-scoped push commits them
together. Fail-closed: a missing skeleton or any API error raises BEFORE the
commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fwgitops.compiler import CompiledChange, SecurityRule

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore


class EnrichError(Exception):
    """Enrichment could not complete. Fail-closed: raised before any push."""


class RuleClient(Protocol):
    """SCM operations enrich drives (real impl = SCM REST API)."""

    def rule_ids_by_name(self, folder: str) -> Dict[str, str]: ...
    def get_rule(self, rule_id: str) -> Dict[str, Any]: ...
    def update_rule(self, rule_id: str, body: Dict[str, Any]) -> None: ...
    def move_rule(
        self, rule_id: str, *, destination: str, rulebase: str, target: Optional[str] = None
    ) -> None: ...


@dataclass(frozen=True)
class EnrichRecord:
    """What was set on one rule — shaped for the evidence bundle."""

    name: str
    application: Tuple[str, ...]
    profile_group: Optional[str]
    log_setting: Optional[str]
    position: str            # e.g. "pre:bottom" | "pre:top"
    moved: bool

    def to_evidence(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "application": list(self.application),
            "profile_group": self.profile_group,
            "log_setting": self.log_setting,
            "position": self.position,
            "moved": self.moved,
        }


@dataclass(frozen=True)
class EnrichResult:
    folder: str
    records: Tuple[EnrichRecord, ...]

    def to_evidence(self) -> Dict[str, Any]:
        return {"folder": self.folder, "records": [r.to_evidence() for r in self.records]}


def restore_deployment_order(
    client: RuleClient, folder: str, expected: Sequence[str],
    rulebase: str = "pre",
) -> List[str]:
    """Put managed rules back into deployment order. Returns the rules moved.

    THE REVERT HALF of order drift. Detection lives in the read-only nightly
    job; the write lives here, behind the same human gate as every other change
    to a live firewall.

    Anchored to the PREVIOUS managed rule rather than moved to `bottom`: moving
    each in turn to the bottom would also drag the whole managed block beneath
    any unmanaged rule sitting below it, changing this platform's relationship
    to config it does not own in the name of fixing our own internal order.

    Idempotent by construction — re-anchoring a rule already in place is a no-op
    at the API, so an apply against a correct rulebase changes nothing.
    `expected` is authoritative; a rule missing from the folder is skipped
    rather than failing, because absence is a DIFFERENT finding the tag engine
    already reports, and failing here would block the apply that fixes it.
    """
    ids = client.rule_ids_by_name(folder)

    # ALREADY IN ORDER? THEN DO NOTHING.
    #
    # The moves are idempotent AT THE API, so issuing them unconditionally
    # looked harmless. It was not: the caller records a remediation whenever
    # this returns a non-empty list, and this returned every rule every time —
    # so a REORDER REMEDIATION RECORD was filed on every apply, claiming rules
    # were re-seated that had never moved. Two false records reached `main`
    # before a dry run made it visible. False evidence is worse than none.
    #
    # `rule_ids_by_name` builds its dict from the API response in order and dicts
    # preserve insertion order, so its keys ARE the live rulebase order.
    present = [n for n in expected if n in ids]
    if [n for n in ids if n in set(expected)] == present:
        return []

    moved: List[str] = []
    for i, name in enumerate(expected):
        if i == 0 or name not in ids or expected[i - 1] not in ids:
            continue
        client.move_rule(ids[name], destination="after", rulebase=rulebase,
                         target=ids[expected[i - 1]])
        moved.append(name)
    return moved


def enrich_folder(
    client: RuleClient, folder: str, changes: Sequence[CompiledChange]
) -> EnrichResult:
    """Write the provider-dropped fields onto every managed rule in `folder`.

    `changes` are the compiled rules for THIS folder (the caller filters). Each
    rule must already exist in SCM (Terraform staged it); a missing one is a
    fail-closed error, never a silent skip — enrich must not run against a folder
    whose skeleton did not apply.
    """
    ids = client.rule_ids_by_name(folder)
    # Deterministic order so a run is reproducible and moves apply predictably.
    ordered = sorted(changes, key=lambda c: c.rule.name)

    # Pass 1 — EXISTENCE ONLY, since v1.16.0. This used to PUT the dropped fields
    # (application / profile_setting / log_setting / source_user / category /
    # negate_* / log_start / description) on every rule, because the provider
    # accepted and silently discarded them.
    #
    # Provider 1.0.12-beta.4 writes them, and the module wires them (ADR-0003
    # addendum), so writing them again here would make TWO writers for one field.
    # That is the ambiguity that produced the profile_setting P1 in the first
    # place, and a redundant write is not harmless: it would silently repair a
    # field Terraform failed to set, so the regression would never surface.
    #
    # Terraform owns the FIELDS. enrich owns ORDERING, which Terraform cannot do
    # from inside a single for_each block — an anchored move needs the anchor's
    # UUID, and `scm_security_rule.this[<key>].id` is a self-reference Terraform
    # rejects with `Error: Cycle`.
    #
    # The existence check stays: enrich must never run against a folder whose
    # skeleton did not apply.
    for ch in ordered:
        r = ch.rule
        if ids.get(r.name) is None:
            raise EnrichError(
                f"rule {r.name!r} not found in folder {folder!r} — did `terraform apply` "
                f"stage it? enrich runs after apply, before push."
            )

    # Pass 2 — apply ORDERING. Separated so every rule exists before any move, and
    # before/after targets resolve deterministically.
    records: List[EnrichRecord] = []
    for ch in ordered:
        r = ch.rule
        moved = _apply_ordering(client, ids[r.name], r, ids)
        records.append(
            EnrichRecord(
                name=r.name, application=tuple(r.application), profile_group=r.profile_group,
                log_setting=r.log_setting, position=_position_str(r), moved=moved,
            )
        )
    return EnrichResult(folder=folder, records=tuple(records))


def _position_str(rule: SecurityRule) -> str:
    """Human/evidence label for the rule's ordering, e.g. "pre:top" or
    "pre:before:REQ-9"."""
    base = f"{rule.rulebase}:{rule.relative_position}"
    return f"{base}:{rule.target_rule}" if rule.target_rule else base


def _apply_ordering(
    client: RuleClient, rule_id: str, rule: SecurityRule, ids: Dict[str, str]
) -> bool:
    """Move the rule into position. Returns True if a Move was issued.

    `top`/`bottom`: absolute (a fresh rule lands at bottom, so bottom is a no-op).
    `before`/`after`: relative to `target_rule`, resolved to a UUID via the folder's
    name→id map — the target may be any rule in the folder (managed or device-
    local). A target that does not resolve is a fail-closed error.
    """
    rel = rule.relative_position
    if rel is None:
        # UNSPECIFIED — the requester expressed no opinion, so enrich must not
        # touch this rule's position. Distinct from `bottom`, which is a request
        # that happens to be satisfied already. Before v1.41.0 the two were the
        # same value and this branch could not exist; without it, `None` falls
        # through to the unknown-ordering error below and fails EVERY apply.
        return False
    if rel == "bottom":
        return False
    if rel == "top":
        client.move_rule(rule_id, destination="top", rulebase=rule.rulebase)
        return True
    if rel in ("before", "after"):
        target_id = ids.get(rule.target_rule) if rule.target_rule else None
        if target_id is None:
            raise EnrichError(
                f"rule {rule.name!r}: ordering target {rule.target_rule!r} not found in the "
                f"folder — cannot place {rel!r} a rule that does not exist."
            )
        client.move_rule(rule_id, destination=rel, rulebase=rule.rulebase, target=target_id)
        return True
    raise EnrichError(f"rule {rule.name!r}: unknown ordering {rel!r}")
