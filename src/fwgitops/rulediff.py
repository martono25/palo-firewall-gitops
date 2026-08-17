"""A managed rule EDITED or DELETED in the console.

WHY THIS EXISTS. The tag engine answers "who owns this?" — unmanaged, orphaned,
malformed. It never asks "does it still MATCH?", so the most common real
unauthorised change, someone altering what an AUTHORISED rule does, produced no
finding at all. `terraform plan` failed the nightly run and printed a diff to a
log that expires: no id, nothing to age, nothing to follow up.

Deleting a managed rule was worse. The tag engine classifies rules that ARE in
SCM, so a rule that is gone is invisible to it entirely — `DriftReport` has no
`missing` member. The state engine has had `unexpected`/`missing`/`modified` for
zones, routes and interfaces since it was written; security rules, the
highest-stakes kind, were the ones without.

COMPARED AGAINST THE COMPILED RULE, NEVER THE INTENT. SCM stores OBJECT NAMES
where the intent writes addresses — a rule declaring `10.20.1.0/24` comes back
as `addr-10.20.1.0_24-85c1076c`. Comparing to the intent's raw values would
report every rule in the estate as modified on the first run. Verified against
the tenant 2026-08-16 (probe run 31941528922), which is also where the excluded
fields below come from.

IT SEES WHAT `terraform plan` CANNOT. The provider treats `application` as
computed so it never fights `enrich` over App-ID, which means an application
edited in the console produces NO plan diff. A direct read compares it like any
other field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: SCM returns these and we never send them, so a difference is not drift.
#: `policy_type` is added by the API; `id` and `folder` are placement metadata.
IGNORED = frozenset({"id", "folder", "policy_type", "snippet", "device", "uuid",
                     "position", "tfid", "scope", "kind"})

#: field on the compiled rule -> field as SCM returns it. Only these are
#: compared: a field absent here is one this platform does not claim to own.
FIELD_MAP: Dict[str, str] = {
    "from_zones": "from",
    "to_zones": "to",
    "sources": "source",
    "destinations": "destination",
    "services": "service",
    "application": "application",
    "action": "action",
    "category": "category",
    "source_user": "source_user",
    "description": "description",
    "log_setting": "log_setting",
    "log_start": "log_start",
    "log_end": "log_end",
    "negate_source": "negate_source",
    "negate_destination": "negate_destination",
    "tags": "tag",
    # DISABLED WAS MISSING UNTIL 2026-08-17, and it is the easiest unauthorised
    # change there is. Toggle a managed rule off in the console and it still
    # EXISTS, still carries its tags, still matches its request name — so the
    # tag engine, the order check and every content field agree that nothing is
    # wrong, while the rule does nothing at all. Disable a deny and a path
    # opens; disable an allow and a service stops.
    #
    # The compiler has always declared it (`False` by default), so this was a
    # plain omission from the map rather than a field nobody could assert.
    "disabled": "disabled",
    # THE THREAT-INSPECTION PROFILE. Strip it in the console and IPS/AV stops
    # applying to the rule while every other field looks identical — the rule
    # still matches the same traffic, it just stops being inspected.
    #
    # The shapes differ: the compiler carries a single group NAME, SCM returns
    # `{"group": ["best-practice"]}`. Verified against the tenant on REQ-2026-0812
    # rather than taken from the Terraform module, which is a claim about what
    # we WRITE, not what the API returns.
    "profile_group": "profile_setting",
}

#: Compared as SETS. SCM does not promise to preserve the order we sent, and for
#: an address or zone list the order carries no meaning — treating it as
#: significant would report drift every time the API reordered a list.
_UNORDERED = frozenset({"from_zones", "to_zones", "sources", "destinations",
                        "services", "application", "category", "source_user",
                        "tags"})


#: Fields every real rule has. Their presence means the row came from a config
#: read rather than a `{folder, name, tags}` provenance snapshot.
_CONTENT_MARKERS = frozenset({"action", "source", "destination", "service",
                              "from", "to"})


@dataclass(frozen=True)
class FieldDiff:
    field: str
    declared: Any
    actual: Any

    def __str__(self) -> str:
        return f"{self.field}: declared {self.declared!r}, live {self.actual!r}"


@dataclass(frozen=True)
class RuleDiff:
    name: str
    scope: str
    missing: bool                     # declared in Git, absent from SCM
    fields: Tuple[FieldDiff, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.missing and not self.fields

    def summary(self) -> str:
        if self.missing:
            return (f"  missing  {self.scope}/{self.name} — declared in Git, "
                    f"absent from SCM")
        return (f"  modified {self.scope}/{self.name} — "
                + "; ".join(str(f) for f in self.fields))


def _profile_group(value: Any) -> Any:
    """SCM's `{"group": ["best-practice"]}` -> the group name the compiler holds."""
    if isinstance(value, dict):
        groups = value.get("group") or []
        return groups[0] if groups else None
    return value


def _norm(name: str, value: Any) -> Any:
    if name == "profile_group":
        return _profile_group(value)
    if name in _UNORDERED:
        return frozenset(value or ())
    if value == "":
        # SCM returns an unset description as "" and the compiler carries None.
        # Reporting that pair as drift would flag every rule with no
        # description, which is most of them.
        return None
    return value


def carries_content(row: Dict[str, Any]) -> bool:
    """Does this row hold rule CONFIG, or only provenance?

    A `{folder, name, tags}` snapshot is enough for the tag engine and carries
    nothing to compare. Without this check every declared rule reads as modified
    against an empty field — a confident, total false positive, which is how a
    detector gets switched off in its first week.

    Distinguishing "SCM says this field is empty" from "this snapshot never had
    fields" cannot be done per field, so it is done per ROW: a row holding none
    of the CONFIG keys is not a content snapshot.

    `tag` is deliberately NOT a marker even though it is compared. A
    provenance-only snapshot carries tags — that is its entire purpose — so
    counting them made every thin snapshot look comparable and reported each
    rule as modified against a dozen empty fields.
    """
    return any(k in row for k in _CONTENT_MARKERS)


def compare(rule: Any, live: Optional[Dict[str, Any]], *, scope: str) -> RuleDiff:
    """One declared rule against what SCM holds for it.

    `live` is None when SCM has no rule by that name — DELETED, which the tag
    engine cannot see because it only classifies rules that exist.
    """
    if live is None:
        return RuleDiff(name=getattr(rule, "name", "?"), scope=scope, missing=True)
    if not carries_content(live):
        # NOT COMPARABLE, which is different from CLEAN. The caller is told
        # once, loudly, rather than each rule quietly reporting no drift.
        return RuleDiff(name=rule.name, scope=scope, missing=False)

    diffs: List[FieldDiff] = []
    for mine, theirs in sorted(FIELD_MAP.items()):
        if theirs in IGNORED:
            continue
        want = _norm(mine, getattr(rule, mine, None))
        got = _norm(mine, live.get(theirs))
        # A FIELD THIS PLATFORM DOES NOT DECLARE IS NOT COMPARED.
        #
        # The first live run reported EVERY managed rule as modified:
        # `log_setting: declared None, live 'Cortex Data Lake'`. No intent
        # declares log forwarding, so the compiler emits None — while SCM holds
        # a value nothing here wrote and Terraform cannot clear (the field is
        # optional-computed, so a null config means "leave alone", not
        # "remove"). Comparing it produced drift that no remediation could fix:
        # a permanently red job, which is how a detector gets switched off.
        #
        # THE TRADE, stated rather than hidden: a field left undeclared can be
        # SET in the console without this noticing. The remedy is to declare it —
        # `spec.log_forwarding` exists and is validated against
        # catalog/log-forwarding.yaml — at which point it is compared like any
        # other field. Asserting a value this platform never writes is the worse
        # error, because it cannot be satisfied.
        if want is None:
            continue
        if want != got:
            diffs.append(FieldDiff(field=theirs,
                                   declared=sorted(want) if isinstance(want, frozenset) else want,
                                   actual=sorted(got) if isinstance(got, frozenset) else got))
    return RuleDiff(name=rule.name, scope=scope, missing=False, fields=tuple(diffs))


def compare_all(rules: Iterable[Any], live_rows: Iterable[Dict[str, Any]], *,
                scope: str) -> List[RuleDiff]:
    """Every declared rule for one scope. Returns only the ones that differ."""
    by_name = {str(r.get("name")): r for r in live_rows
               if isinstance(r, dict) and r.get("name")}
    out = []
    for rule in rules:
        d = compare(rule, by_name.get(rule.name), scope=scope)
        if not d.is_clean:
            out.append(d)
    return out
