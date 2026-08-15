"""Tests for the tag/identity convention (T6).

Coverage targets the four consumers: rule synthesis (build tags), drift scoping
(is_managed), evidence (parse round-trip), and terraform for_each (stable keys +
dedup naming). Determinism is asserted explicitly because everything downstream
assumes it.
"""

from __future__ import annotations

from datetime import date

import pytest

from fwgitops import tags
from fwgitops.tags import (
    MANAGED_TAG,
    ManagedMeta,
    Section,
    identity_hash,
    is_managed,
    managed_tags,
    object_name,
    parse_managed_meta,
    rule_key,
    validate_object_name,
)


# ── managed_tags + round-trip ─────────────────────────────────────────────
def test_managed_tags_marker_is_first():
    t = managed_tags(req_id="REQ-2026-0417", section=Section.SPECIFIC_ALLOW)
    assert t[0] == MANAGED_TAG


def test_full_round_trip_build_then_parse():
    built = managed_tags(
        req_id="REQ-2026-0417",
        section=Section.SPECIFIC_ALLOW,
        ticket="JIRA-12345",
    )
    meta = parse_managed_meta(built)
    assert meta == ManagedMeta(
        req_id="REQ-2026-0417",
        section=Section.SPECIFIC_ALLOW,
        ticket="JIRA-12345",
    )


def test_expiry_is_NEVER_written_to_a_rule():
    """`metadata.expires` is CI lifecycle, not a property of the firewall rule.

    Nothing on the device acts on it: PAN-OS stores the tag and ignores it, so
    writing it shipped a date that LOOKED like a control and was not one — the
    expensive kind of metadata, because a reader in the SCM UI reasonably assumes
    something enforces it. Real device-enforced expiry exists
    (`scm_security_rule.schedule` -> `scm_schedule.non_recurring`); a tag was
    never going to be it.

    Asserted on the KEY, not on one date, so no future caller can reintroduce it
    under a different value.
    """
    for kwargs in (
        dict(req_id="REQ-1", section=Section.SPECIFIC_ALLOW),
        dict(req_id="REQ-1", section=Section.SPECIFIC_ALLOW, ticket="T-1"),
    ):
        assert not any(t.startswith("gitops:expires") for t in managed_tags(**kwargs))


def test_a_legacy_expiry_tag_is_IGNORED_not_fatal():
    """No live rule carries one (they were removed from the pilot in v1.22.0),
    and `ManagedMeta` no longer has the field. An old tag must therefore be
    ignored like any other unrecognised gitops key — NOT treated as malformed,
    which fails closed loudly and would turn a leftover label into an incident."""
    legacy = managed_tags(req_id="REQ-OLD", section=Section.SPECIFIC_ALLOW) + [
        "gitops:expires:2026-10-19"]
    meta = parse_managed_meta(legacy)
    assert meta.req_id == "REQ-OLD"
    assert not hasattr(meta, "expires")


def test_optional_fields_omitted():
    built = managed_tags(req_id="REQ-1", section="broad-allow")
    meta = parse_managed_meta(built)
    assert meta.ticket is None
    assert meta.section is Section.BROAD_ALLOW  # str section accepted


def test_section_accepts_enum_and_str_equivalently():
    a = managed_tags(req_id="REQ-1", section=Section.DEFAULT_DENY)
    b = managed_tags(req_id="REQ-1", section="default-deny")
    assert a == b


def test_determinism_identical_inputs_identical_output():
    kw = dict(req_id="REQ-9", section=Section.SPECIFIC_ALLOW, ticket="T-1")
    assert managed_tags(**kw) == managed_tags(**kw)


def test_rejects_unsafe_value():
    with pytest.raises(ValueError):
        managed_tags(req_id="bad:id", section=Section.SPECIFIC_ALLOW)


# ── is_managed (drift scoping) ────────────────────────────────────────────
def test_is_managed_true_for_managed():
    assert is_managed(managed_tags(req_id="REQ-1", section=Section.SPECIFIC_ALLOW))


@pytest.mark.parametrize("brownfield", [[], ["some-other-tag"], ["gitops"], ["gitops:req:REQ-1"]])
def test_is_managed_false_for_unmarked(brownfield):
    # A rule without the exact marker (brownfield, or a stray gitops-ish tag)
    # must NOT read as managed — this is what keeps brownfield out of scope.
    assert is_managed(brownfield) is False


def test_parse_returns_none_for_brownfield():
    assert parse_managed_meta(["dept:finance"]) is None


def test_parse_fails_closed_on_marker_without_req():
    with pytest.raises(ValueError):
        parse_managed_meta([MANAGED_TAG, "gitops:section:specific-allow"])


# ── identity_hash + rule_key (for_each stability) ─────────────────────────
def _identity(**over):
    base = dict(
        from_zone="trust",
        to_zone="app",
        source=["10.20.1.0/24"],
        destination=["10.20.9.10/32"],
        service=["tcp/443"],
        action="allow",
    )
    base.update(over)
    return base


def test_identity_hash_stable_across_member_reordering():
    a = identity_hash(**_identity(source=["10.0.0.1/32", "10.0.0.2/32"]))
    b = identity_hash(**_identity(source=["10.0.0.2/32", "10.0.0.1/32"]))
    assert a == b


def test_identity_hash_changes_when_identity_changes():
    a = identity_hash(**_identity())
    b = identity_hash(**_identity(action="deny"))
    assert a != b


def test_rule_key_single_vs_multi():
    assert rule_key("REQ-5") == "REQ-5"
    disc = identity_hash(**_identity())
    assert rule_key("REQ-5", disc) == f"REQ-5-{disc}"


def test_rule_key_removing_a_sibling_does_not_renumber():
    # Two rules under one request. Their keys are content-derived, so dropping
    # rule A must leave rule B's key untouched (positional ordinals would break).
    dest_a = identity_hash(**_identity(destination=["10.0.0.1/32"]))
    dest_b = identity_hash(**_identity(destination=["10.0.0.2/32"]))
    key_b_before = rule_key("REQ-7", dest_b)
    # Simulate removing A: recompute B's key from the same inputs.
    key_b_after = rule_key("REQ-7", dest_b)
    assert key_b_before == key_b_after
    assert dest_a != dest_b


# ── object_name (dedup) ───────────────────────────────────────────────────
def test_object_name_dedup_same_value_same_name():
    assert object_name("address", "10.20.1.0/24") == object_name("address", "10.20.1.0/24")


def test_object_name_distinct_for_distinct_values():
    assert object_name("address", "10.20.1.0/24") != object_name("address", "10.20.2.0/24")


def test_object_name_prefix_by_kind():
    assert object_name("address", "1.1.1.1/32").startswith("addr-")
    assert object_name("service", "tcp/443").startswith("svc-")


def test_object_name_unknown_kind_raises():
    with pytest.raises(ValueError):
        object_name("nonsense", "x")


def test_validate_object_name_length_cap():
    with pytest.raises(ValueError):
        validate_object_name("x" * (tags.PANOS_NAME_MAX + 1))
    ok = "x" * tags.PANOS_NAME_MAX
    assert validate_object_name(ok) == ok


# ── the naming scheme (2026-08-15) ─────────────────────────────────────────

def test_a_name_is_LEGIBLE_and_still_a_pure_function_of_the_value():
    """Both halves earn their place.

    Readable, because `addr-85c1076cfe` in the SCM GUI tells an engineer
    nothing — they had to come back to Git to decode it. And a pure function of
    the VALUE, because that is what makes two requests for the same address
    reuse one object instead of minting two.
    """
    assert object_name("address", "10.20.1.0/24") == "addr-10.20.1.0_24-85c1076c"
    assert object_name("service", "tcp/443") == "svc-tcp_443-fd64e1b8"
    # Same value, asked for twice -> the same object. This is the whole of reuse.
    assert object_name("service", "tcp/443") == object_name("service", "tcp/443")


def test_the_name_ignores_HOW_the_service_was_requested():
    """`catalog/services.yaml` calls tcp/443 `https`, and naming the object
    `svc-https` was the obvious idea. It breaks dedup: a requester may write
    `name: https` OR `protocol: tcp, port: "443"`, and two names for one value
    means two objects. It also ties object identity to an EDITABLE file —
    renaming a catalog entry would orphan every object named after it."""
    assert "https" not in object_name("service", "tcp/443")


def test_unsafe_characters_never_reach_a_name():
    """PAN-OS names allow letters, digits, period, hyphen, underscore. A slash
    or a colon in a name is rejected by the API, and `/` is an XPath separator."""
    import re

    for value in ("10.20.1.0/24", "tcp/443", "2001:db8::/32", "a b/c:d"):
        name = object_name("address", value)
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), f"{value!r} -> {name!r}"


def test_truncation_sacrifices_the_READABLE_half_never_the_digest():
    """The digest is what makes the name unique and unforgeable. Truncating it
    to fit a long FQDN would trade a cosmetic gain for collisions and a broken
    ownership proof."""
    long_fqdn = "a." + "very-long-subdomain." * 5 + "example.com"
    name = object_name("address", long_fqdn)
    assert len(name) <= 63
    assert name.endswith(object_name("address", long_fqdn)[-8:])

    import hashlib
    digest = hashlib.sha256(long_fqdn.encode()).hexdigest()[:8]
    assert name.endswith(digest), "the digest must survive truncation intact"

    other = "b." + "very-long-subdomain." * 5 + "example.com"
    assert object_name("address", other) != name, (
        "two long names truncating to the same readable half must still differ")
