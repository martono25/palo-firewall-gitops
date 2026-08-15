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

    ── NO EXPIRY, AND NO EXPIRY FIELD AT ALL (v1.23.0) ────────────────────
    `metadata.expires` was briefly written here as `gitops:expires:<date>`, then
    removed from the tags (v1.22.0) and finally from the schema entirely
    (v1.23.0). It modelled a lifecycle this platform does not run: nothing on the
    firewall acts on such a tag, and no job ever removed an expired rule.

    PAN-OS does have real, device-enforced rule expiry —
    `scm_security_rule.schedule` pointing at an `scm_schedule` with
    `non_recurring` date ranges. If that is ever wanted, that is the mechanism; a
    tag was never going to be it.
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

    if req_id is None:
        # Marker present but no req tag — malformed. Surface loudly rather than
        # silently returning a half-populated record (fail closed).
        raise ValueError("managed marker present but no gitops:req tag found")

    return ManagedMeta(req_id=req_id, section=section, ticket=ticket)


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


#: Everything outside the PAN-OS-safe charset collapses to this in a name.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Hash characters kept. Short, because the readable half carries the meaning
#: and the digest only has to make the name unforgeable and unique.
_DIGEST_LEN = 8


def readable_part(value: str) -> str:
    """The legible half of an object name: the VALUE, charset-safe.

    `10.20.1.0/24` -> `10.20.1.0_24`, `tcp/443` -> `tcp_443`. Periods, hyphens
    and underscores are all PAN-OS-safe (the same charset `_SAFE_VALUE` already
    enforces for tag values), so an IPv4 CIDR survives almost intact and stays
    recognisable at a glance in the SCM GUI.

    DERIVED FROM THE VALUE, NEVER FROM A CATALOG NAME. `catalog/services.yaml`
    knows `tcp/443` as `https`, and naming the object `svc-https` was the
    obvious idea — but it fails twice. A requester may write either
    `name: https` or `protocol: tcp, port: "443"`, and if those produced
    different names the same value would mint two objects and dedup would break.
    And object identity would then depend on an EDITABLE file: renaming a
    catalog entry would silently orphan every object named after it.
    """
    out = _UNSAFE_IN_NAME.sub("_", value).strip("._-")
    return out or "x"


def legacy_object_name(kind: str, value: str) -> str:
    """The name this platform minted BEFORE 2026-08-15: `<prefix>-<sha256[:10]>`.

    Kept for one reason: `objectsweep.is_ours` proves ownership by re-deriving a
    name from its value, so the day the scheme changed, every object already in
    the tenant stopped matching and became unrecognisable — "not ours", which
    the sweep refuses to touch. Eleven objects would have been orphaned
    permanently, and the count would have looked like a foreign-object tally
    rather than our own litter.

    DELETE THIS once no tenant holds a legacy name. Until then it is what lets
    the sweep clean up after the rename.

    HOW TO KNOW IT IS SAFE TO DELETE: run the probe against
    `/config/objects/v1/addresses` and `/config/objects/v1/services` for every
    folder and confirm no name matches the legacy shape `<prefix>-<10 hex>`.
    `prod-edge` was clean as of 2026-08-15, the day of the rename, because the
    sweep removed all eleven in the same run that created their replacements.
    Any tenant onboarded before that date has to be checked separately.
    """
    if kind not in _OBJECT_PREFIX:
        raise ValueError(f"unknown object kind {kind!r}; expected one of {list(_OBJECT_PREFIX)}")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{_OBJECT_PREFIX[kind]}-{digest}"


def object_name(kind: str, value: str, length: int = _DIGEST_LEN) -> str:
    """Deterministic, dedup-friendly, and LEGIBLE object name for a value.

    Shape: `<prefix>-<readable>-<digest>` — e.g. `addr-10.20.1.0_24-85c1076c`,
    `svc-tcp_443-fd64e1b8`.

    Same value → same name, so re-requesting an address or service reuses the
    existing object instead of creating a duplicate. That property is why the
    name is a pure function of the value and of nothing else.

    THE DIGEST IS NOT DECORATION. `objectsweep.is_ours` proves an object was
    minted here by checking that its name equals the name its own value hashes
    to, and the sweep DELETES on the strength of that. A purely readable name is
    exactly what a human would type by hand for the same address, so a
    hand-made `addr-10.20.1.0_24` would look like ours and be swept. The digest
    is what a hand-made name cannot reproduce.

    The readable half is truncated to fit PAN-OS's 63-char cap; the digest is
    never truncated, so uniqueness survives any truncation.
    """
    if kind not in _OBJECT_PREFIX:
        raise ValueError(f"unknown object kind {kind!r}; expected one of {list(_OBJECT_PREFIX)}")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    prefix = _OBJECT_PREFIX[kind]
    budget = PANOS_NAME_MAX - len(prefix) - len(digest) - 2   # two separators
    readable = readable_part(value)[:budget].rstrip("._-")
    name = f"{prefix}-{readable}-{digest}" if readable else f"{prefix}-{digest}"
    return validate_object_name(name)


def validate_object_name(name: str) -> str:
    """Enforce the PAN-OS object-name length cap. Returns the name unchanged."""
    if len(name) > PANOS_NAME_MAX:
        raise ValueError(f"object name {name!r} exceeds PAN-OS max of {PANOS_NAME_MAX} chars")
    return name
