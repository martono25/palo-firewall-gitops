"""SCM enrich step — writes the security-rule fields the Terraform provider drops.

Live finding (2026-07-28): the `paloaltonetworks/scm` provider (v1.0.11 AND
1.0.12-beta.3) accepts but does NOT write `application`, `profile_setting`,
`log_setting`, or ordering on security rules — it treats them as computed and
drops config input (a fresh create lands `["any"]` / no profile / SCM's default
log-forwarding / bottom). The SCM REST API honors every one of them, proven by a
live POST→GET round-trip. So, mirroring how `push` commits what the provider
can't, `enrich` SETS what the provider can't:

    terraform apply ──▶ rule skeleton staged (zones/addr/svc/action/tags)
         enrich_folder() ──▶ application / profile_setting / log_setting / ordering
             push ──▶ commit (skeleton + enrichment together, one atomic change)

The seam is post-apply / pre-push: the rule already exists (so this UPDATES, never
creates), and enrich's writes land in the SAME candidate as Terraform's, so the
single admin-scoped push commits skeleton + enrichment together. Fail-closed: a
missing skeleton or any API error raises BEFORE the commit.

Idempotent by construction (GET-modify-PUT with the desired values converges), and
NON-DESTRUCTIVE for opt-in fields: `profile`/`log_forwarding` are written only when
the intent declares them; when absent, the rule's existing value is preserved (an
omitted field means "not managed", per ADR-0003 — not "clear it"). `application`
always reflects the declared desired state (it defaults to `["any"]`), so drift is
reconciled.

SCM calls sit behind the `RuleClient` protocol so the orchestration is unit-
testable against a fake; the real client (SCM REST API) is thin glue in clients.py.
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

    # Pass 1 — set the dropped FIELDS on every rule. Done first so that when Move
    # (pass 2) references another managed rule as a before/after target, that
    # target already exists and is fully enriched.
    for ch in ordered:
        r = ch.rule
        rule_id = ids.get(r.name)
        if rule_id is None:
            raise EnrichError(
                f"rule {r.name!r} not found in folder {folder!r} — did `terraform apply` "
                f"stage it? enrich runs after apply, before push."
            )
        client.update_rule(rule_id, _merged_body(client.get_rule(rule_id), r))

    # Pass 2 — apply ORDERING. Separated so all rules exist/are enriched before any
    # move, and before/after targets resolve deterministically.
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


def _merged_body(current: Dict[str, Any], rule: SecurityRule) -> Dict[str, Any]:
    """GET-modify body: set the provider-dropped fields, preserve everything else.

    `application` always reflects the declared desired state (defaults to
    ["any"]). `profile_setting`/`log_setting` are OPT-IN: written only when the
    intent declared them; when absent the current value is preserved (omitted =
    "not managed", not "clear" — the non-destructive rule from ADR-0003). Drops
    server-only keys the PUT rejects.
    """
    body = dict(current)
    body["application"] = list(rule.application)
    if rule.profile_group:
        body["profile_setting"] = {"group": [rule.profile_group]}
    if rule.log_setting:
        body["log_setting"] = rule.log_setting
    # v1.0 completeness. These carry declared defaults (any / false), so they
    # always reflect desired state — set unconditionally (like application). The
    # provider drops config-driven fields, so enrich is authoritative; `action` is
    # re-asserted here to guarantee drop/reset-* land even if the provider mangles
    # it. `description` is opt-in (set only when declared) to stay non-destructive.
    body["action"] = rule.action
    body["source_user"] = list(rule.source_user)
    body["category"] = list(rule.category)
    body["negate_source"] = rule.negate_source
    body["negate_destination"] = rule.negate_destination
    body["log_start"] = rule.log_start
    if rule.description is not None:
        body["description"] = rule.description
    for k in ("id", "tfid"):
        body.pop(k, None)
    return body


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
