"""Tag / identity convention — the shared contract (design task T6).

This module is the single source of truth for how a gitops-managed object or
rule is *marked*, *identified*, and *keyed*. Four subsystems depend on it, so it
lives in exactly one place:

    managed_tags()  ─┬─▶  rule synthesis   (compiler stage 7: tag every rule/object)
                     ├─▶  for_each keys    (terraform: stable identity, no churn)
    is_managed()  ───┼─▶  drift scoping    (detector: managed vs brownfield)
    parse_meta()  ───┴─▶  evidence bundle  (audit: who/what/why/expiry)

Design decisions encoded here (see docs/DESIGN.md):

  * Metadata rides on PAN-OS tags under a single namespace ("gitops:...").
    The `gitops:managed` marker is what drift scoping keys on, so an
    out-of-band rule (no marker) in a managed folder reads as additive drift
    while brownfield rules are ignored.

  * `for_each` keys are STABLE. A rule's key is `req_id` (+ a content-derived
    discriminator when one request yields multiple rules). Stability is against
    *reordering* and *unrelated edits*: removing one rule from a multi-rule
    request does not renumber the others (positional ordinals would), and
    editing a non-identity field (log, description) does not change the key.
    Changing a rule's semantic identity (zones/src/dst/service/action) is, by
    definition, a different rule and intentionally re-keys.

  * Object names are DETERMINISTic and dedup-friendly: the same value always
    maps to the same name, so re-requesting an address reuses the object
    instead of creating a duplicate (stops sprawl — compiler stage 5).

Known constraint to verify against the live platform: PAN-OS/SCM object names
cap at 63 chars and tag names at 127; `validate_object_name` enforces the
former. The exact tag-name charset should be confirmed during the `scm`
provider spike before Phase 1 (see docs/DESIGN.md → The Assignment).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable, Optional, Sequence, Union

# ── Namespace ─────────────────────────────────────────────────────────────
NAMESPACE = "gitops"
SEP = ":"
MANAGED_TAG = f"{NAMESPACE}{SEP}managed"

# PAN-OS limits (verify against live SCM during the provider spike).
PANOS_NAME_MAX = 63
PANOS_TAG_MAX = 127

# REQ ids / tickets are constrained to a safe charset so tag round-tripping is
# unambiguous (values never contain the SEP).
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")


class Section(str, Enum):
    """Rulebase sections, top-to-bottom evaluation order (design: placement)."""

    INFRA_DENY = "infra-deny"
    SPECIFIC_ALLOW = "specific-allow"
    BROAD_ALLOW = "broad-allow"
    DEFAULT_DENY = "default-deny"


#: Placement order, top (evaluated first) to bottom.
SECTION_ORDER: tuple[Section, ...] = (
    Section.INFRA_DENY,
    Section.SPECIFIC_ALLOW,
    Section.BROAD_ALLOW,
    Section.DEFAULT_DENY,
)


@dataclass(frozen=True)
class ManagedMeta:
    """Metadata extracted from a managed object/rule's tag set."""

    req_id: str
    section: Optional[Section]
    ticket: Optional[str]
    expires: Optional[date]


def _validate_value(kind: str, value: str) -> str:
    if not _SAFE_VALUE.match(value):
        raise ValueError(
            f"{kind} value {value!r} must match {_SAFE_VALUE.pattern} "
            f"(no {SEP!r} — it is the tag separator)"
        )
    return value


def _tag(key: str, value: str) -> str:
    return f"{NAMESPACE}{SEP}{key}{SEP}{value}"


def managed_tags(
    *,
    req_id: str,
    section: Union[Section, str],
    ticket: Optional[str] = None,
) -> list[str]:
    """Build the canonical tag set for a managed object/rule.

    The marker tag is always first. Deterministic: identical inputs yield an
    identical, order-stable list.

    ── EXPIRY IS DELIBERATELY NOT HERE (removed 2026-08-05) ────────────────
    `metadata.expires` used to be written as `gitops:expires:<date>`. It is not
    any more, because it is CI EXPIRY, NOT RULE EXPIRY: nothing on the firewall
    acts on it. PAN-OS stores the tag and ignores it, so on the device it was a
    date that looked like a control and was not one — the most expensive kind of
    metadata, because a reader in the SCM UI reasonably assumes something
    enforces it.

    A rule's tags describe WHAT THE RULE IS. Expiry describes what this pipeline
    intends to DO with the request later, which is a property of the request, not
    of the firewall object. It lives in the intent YAML and the evidence bundle,
    which is where lifecycle belongs.

    PAN-OS DOES have real rule expiry — `scm_security_rule.schedule` pointing at
    an `scm_schedule` with `non_recurring` date ranges, enforced on the device.
    If device-enforced expiry is ever wanted, that is the mechanism; a tag was
    never going to be.

    CONSEQUENCE, accepted knowingly: an expired-rule check can no longer be
    answered from live state alone. `parse_managed_meta` still READS the tag so
    rules tagged before this change are understood, but nothing writes it, so the
    check must read intent YAML. That trades drift's independence from Git for
    not shipping a date the device implies it honours.
    """
    section_value = section.value if isinstance(section, Section) else Section(section).value
    tags = [
        MANAGED_TAG,
        _tag("req", _validate_value("req_id", req_id)),
        _tag("section", section_value),
    ]
    if ticket is not None:
        tags.append(_tag("ticket", _validate_value("ticket", ticket)))
    return tags


def is_managed(tags: Iterable[str]) -> bool:
    """True iff the tag set carries the gitops-managed marker.

    Drift scoping keys on this: a rule without the marker in a managed folder is
    out-of-band (additive drift); brownfield rules are simply not managed.
    """
    return MANAGED_TAG in set(tags)


def parse_managed_meta(tags: Iterable[str]) -> Optional[ManagedMeta]:
    """Extract ManagedMeta from a tag set, or None if not managed."""
    tag_set = list(tags)
    if not is_managed(tag_set):
        return None

    req_id: Optional[str] = None
    section: Optional[Section] = None
    ticket: Optional[str] = None
    expires: Optional[date] = None

    for tag in tag_set:
        parts = tag.split(SEP, 2)
        if len(parts) != 3 or parts[0] != NAMESPACE:
            continue
        _, key, value = parts
        if key == "req":
            req_id = value
        elif key == "section":
            section = Section(value) if value in Section._value2member_map_ else None
        elif key == "ticket":
            ticket = value
        elif key == "expires":
            # LEGACY READ ONLY. `managed_tags` no longer writes this — see the
            # note there. Kept so a rule tagged before 2026-08-05, or by an older
            # version still deployed somewhere, is parsed rather than treated as
            # malformed. Remove once no live rule carries one.
            expires = date.fromisoformat(value)

    if req_id is None:
        # Marker present but no req tag — malformed. Surface loudly rather than
        # silently returning a half-populated record (fail closed).
        raise ValueError("managed marker present but no gitops:req tag found")

    return ManagedMeta(req_id=req_id, section=section, ticket=ticket, expires=expires)


# ── Deterministic identity ────────────────────────────────────────────────
def _canonical(values: Union[str, Sequence[str]]) -> str:
    if isinstance(values, str):
        return values
    # Sort so member order does not affect identity (a rule with sources
    # [A, B] is the same rule as [B, A]).
    return ",".join(sorted(values))


def identity_hash(
    *,
    from_zone: Union[str, Sequence[str]],
    to_zone: Union[str, Sequence[str]],
    source: Union[str, Sequence[str]],
    destination: Union[str, Sequence[str]],
    service: Union[str, Sequence[str]],
    action: str,
    length: int = 10,
) -> str:
    """Short, order-stable hash of a rule's *semantic identity*.

    Non-identity fields (log, description, tags) are deliberately excluded so
    editing them does not re-key the rule. Reordering set members does not
    change the hash.
    """
    canonical = "|".join(
        [
            _canonical(from_zone),
            _canonical(to_zone),
            _canonical(source),
            _canonical(destination),
            _canonical(service),
            action,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:length]


def rule_key(req_id: str, discriminator: Optional[str] = None) -> str:
    """Stable Terraform `for_each` key for a rule.

    Single rule per request → the req id. Multiple rules → req id plus a
    content-derived discriminator (typically `identity_hash(...)`), so removing
    one rule never renumbers the others.
    """
    _validate_value("req_id", req_id)
    if discriminator is None:
        return req_id
    return f"{req_id}-{discriminator}"


_OBJECT_PREFIX = {
    "address": "addr",
    "address-group": "addrgrp",
    "service": "svc",
    "service-group": "svcgrp",
}


def object_name(kind: str, value: str, length: int = 10) -> str:
    """Deterministic, dedup-friendly object name for a value.

    Same value → same name, so re-requesting an address/service reuses the
    existing object instead of creating a duplicate.
    """
    if kind not in _OBJECT_PREFIX:
        raise ValueError(f"unknown object kind {kind!r}; expected one of {list(_OBJECT_PREFIX)}")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    name = f"{_OBJECT_PREFIX[kind]}-{digest}"
    return validate_object_name(name)


def validate_object_name(name: str) -> str:
    """Enforce the PAN-OS object-name length cap. Returns the name unchanged."""
    if len(name) > PANOS_NAME_MAX:
        raise ValueError(f"object name {name!r} exceeds PAN-OS max of {PANOS_NAME_MAX} chars")
    return name
