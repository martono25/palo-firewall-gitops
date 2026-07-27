"""Tests for the Phase-2 risk classifier (policy-as-code, fail-closed)."""

from __future__ import annotations

import pytest

from fwgitops.classify import CLASSIFIER_VERSION, DEFAULT_THRESHOLDS, Thresholds, classify
from fwgitops.compiler import AddressObject, CompiledChange, SecurityRule, ServiceObject


def _change(*, srcs, dsts, services, from_zones=("local",), to_zones=("internet",),
            action="allow", name="R", folder="f"):
    """Build a CompiledChange.

    srcs/dsts: list of (name, type, value); services: list of (name, proto, port).
    """
    addr = {}

    def add(items):
        names = []
        for name, typ, val in items:
            addr[name] = AddressObject(name=name, type=typ, value=val, folder="f", tags=[])
            names.append(name)
        return names

    src_names, dst_names = add(srcs), add(dsts)
    svcs = [ServiceObject(name=n, protocol=p, port=pt, folder="f", tags=[]) for n, p, pt in services]
    rule = SecurityRule(
        name=name, folder=folder, from_zones=list(from_zones), to_zones=list(to_zones),
        sources=src_names, destinations=dst_names, services=[s.name for s in svcs],
        action=action, log_end=True, tags=[],
    )
    return CompiledChange(address_objects=list(addr.values()), service_objects=svcs, rule=rule)


HOST = ("d", "ip-netmask", "10.20.9.10/32")
HTTPS = ("https", "tcp", "443")


def _fired(v):
    return {f["check"] for f in v.checks_fired}


# ── LOW: specific, nothing fires ──────────────────────────────────────────
def test_specific_change_is_low():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS]))
    assert v.tier == "LOW"
    assert v.checks_fired == ()
    assert v.classifier_version == CLASSIFIER_VERSION
    assert v.thresholds_version == DEFAULT_THRESHOLDS.version


def test_fqdn_source_is_specific_not_broad():
    v = classify(_change(srcs=[("s", "fqdn", "app.internal")], dsts=[HOST], services=[HTTPS]))
    assert v.tier == "LOW"


# ── CRITICAL ──────────────────────────────────────────────────────────────
def test_any_any_allow_is_critical():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "0.0.0.0/0")], dsts=[("d", "ip-netmask", "0.0.0.0/0")],
        services=[HTTPS],
    ))
    assert v.tier == "CRITICAL" and "any_any_allow" in _fired(v)
    assert v.is_dual_control


def test_any_inbound_from_internet_is_critical():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "0.0.0.0/0")], dsts=[HOST], services=[HTTPS],
        from_zones=("untrust",), to_zones=("local",),
    ))
    assert v.tier == "CRITICAL" and "inbound_any_from_internet" in _fired(v)


# ── HIGH ──────────────────────────────────────────────────────────────────
def test_broad_source_is_high():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.0.0.0/8")], dsts=[HOST], services=[HTTPS]))
    assert v.tier == "HIGH" and "broad_source" in _fired(v)


def test_broad_destination_is_high():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[("d", "ip-netmask", "10.0.0.0/12")],
        services=[HTTPS],
    ))
    assert v.tier == "HIGH" and "broad_destination" in _fired(v)


def test_wide_port_range_is_high():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[("all", "tcp", "0-65535")],
    ))
    assert v.tier == "HIGH" and "any_service" in _fired(v)


def test_risky_port_exposed_from_internet_is_high():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "203.0.113.0/24")], dsts=[HOST], services=[("rdp", "tcp", "3389")],
        from_zones=("untrust",), to_zones=("local",),
    ))
    assert v.tier == "HIGH" and "risky_port_from_internet" in _fired(v)


def test_risky_port_internal_is_not_flagged():
    # Same RDP port but NOT from an internet zone -> not the risky-exposure check.
    v = classify(_change(
        srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[("rdp", "tcp", "3389")],
        from_zones=("local",), to_zones=("local",),
    ))
    assert v.tier == "LOW"


# ── Deny reduces attack surface ───────────────────────────────────────────
def test_deny_any_any_is_not_critical():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "0.0.0.0/0")], dsts=[("d", "ip-netmask", "0.0.0.0/0")],
        services=[HTTPS], action="deny",
    ))
    assert v.tier == "LOW"  # a broad deny doesn't open the surface


# ── Most-severe wins ──────────────────────────────────────────────────────
def test_tier_is_most_severe_fired_check():
    v = classify(_change(
        srcs=[("s", "ip-netmask", "0.0.0.0/0")], dsts=[("d", "ip-netmask", "0.0.0.0/0")],
        services=[("all", "tcp", "0-65535")],
    ))
    # any_any (CRITICAL) + broad? no (any handled separately) + any_service (HIGH) -> CRITICAL
    assert v.tier == "CRITICAL"
    assert {"any_any_allow", "any_service"} <= _fired(v)


# ── Fail closed ───────────────────────────────────────────────────────────
def test_unresolvable_object_escalates_to_high():
    ch = _change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS])
    # Rule references a source that isn't among the address objects.
    broken = CompiledChange(
        address_objects=[a for a in ch.address_objects if a.name != "s"],
        service_objects=ch.service_objects, rule=ch.rule,
    )
    v = classify(broken)
    assert v.tier == "HIGH" and "unclassifiable_input" in _fired(v)


def test_unparseable_cidr_escalates_to_high():
    v = classify(_change(srcs=[("s", "ip-netmask", "not-a-cidr")], dsts=[HOST], services=[HTTPS]))
    assert v.tier == "HIGH" and "unclassifiable_input" in _fired(v)


def test_custom_thresholds_version_recorded():
    th = Thresholds(version="test-1", broad_prefix_max=24)
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS]),
                 thresholds=th)
    # /24 <= 24 is now "broad" under these thresholds
    assert v.tier == "HIGH" and v.thresholds_version == "test-1"


# ── Stateful checks (policy context) ──────────────────────────────────────
from fwgitops.classify import PolicyContext  # noqa: E402


def _spec(name, fz, tz, src="10.20.1.0/24"):
    return _change(srcs=[("s", "ip-netmask", src)], dsts=[HOST], services=[HTTPS],
                   from_zones=(fz,), to_zones=(tz,), name=name)


def test_novel_zone_pair_fires_for_a_new_path():
    a = _spec("A", "local", "internet")
    b = _spec("B", "dmz", "app")          # nobody else uses dmz->app
    policy = PolicyContext.from_changes([a, b])
    v = classify(b, policy=policy)
    assert v.tier == "HIGH" and "novel_zone_pair" in _fired(v)


def test_shared_zone_pair_is_not_novel():
    a = _spec("A", "local", "internet")
    b = _spec("B", "local", "internet", src="10.20.2.0/24")  # same pair as A
    policy = PolicyContext.from_changes([a, b])
    assert classify(b, policy=policy).tier == "LOW"          # A shares the pair
    assert "novel_zone_pair" not in _fired(classify(b, policy=policy))


def test_lone_rule_is_novel_against_itself_only():
    a = _spec("A", "local", "internet")
    v = classify(a, policy=PolicyContext.from_changes([a]))  # only A uses the pair
    assert v.tier == "HIGH" and "novel_zone_pair" in _fired(v)


def test_redundant_rule_flagged_low():
    a = _spec("A", "local", "internet")
    b = _spec("B", "local", "internet")   # identical match to A (diff id)
    policy = PolicyContext.from_changes([a, b])
    v = classify(b, policy=policy)
    assert "redundant_rule" in _fired(v)
    # redundancy is a hygiene note, not a risk escalation
    assert v.tier == "LOW"


def test_no_policy_means_no_stateful_checks():
    b = _spec("B", "dmz", "app")
    assert classify(b).tier == "LOW"      # without policy, novel_zone_pair can't fire


# ── Shadowing (subset of a broader rule) ──────────────────────────────────
def test_shadowed_by_broader_rule():
    narrow = _change(srcs=[("sn", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS], name="NARROW")
    broad = _change(srcs=[("sb", "ip-netmask", "10.0.0.0/8")], dsts=[HOST], services=[HTTPS], name="BROAD")
    policy = PolicyContext.from_changes([narrow, broad])
    assert "shadowed_by" in _fired(classify(narrow, policy=policy))  # narrow ⊂ broad


def test_broad_rule_is_not_shadowed_by_narrow():
    narrow = _change(srcs=[("sn", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS], name="NARROW")
    broad = _change(srcs=[("sb", "ip-netmask", "10.0.0.0/8")], dsts=[HOST], services=[HTTPS], name="BROAD")
    policy = PolicyContext.from_changes([narrow, broad])
    assert "shadowed_by" not in _fired(classify(broad, policy=policy))  # broad ⊄ narrow


def test_wider_service_shadows_narrower():
    narrow = _change(srcs=[("sn", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                     services=[("svc443", "tcp", "443")], name="N")
    broad = _change(srcs=[("sb", "ip-netmask", "10.0.0.0/8")], dsts=[HOST],
                    services=[("svcall", "tcp", "0-65535")], name="B")
    policy = PolicyContext.from_changes([narrow, broad])
    assert "shadowed_by" in _fired(classify(narrow, policy=policy))  # 443 ⊂ 0-65535


def test_not_shadowed_when_service_not_covered():
    # the "narrow" rule uses port 22, which the broader rule (443 only) doesn't cover
    a = _change(srcs=[("sa", "ip-netmask", "10.0.0.0/8")], dsts=[HOST],
                services=[("svc443", "tcp", "443")], name="A")
    b = _change(srcs=[("sb", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                services=[("svc22", "tcp", "22")], name="B")
    policy = PolicyContext.from_changes([a, b])
    assert "shadowed_by" not in _fired(classify(b, policy=policy))


def test_not_shadowed_across_zone_pairs():
    inside = _change(srcs=[("si", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS],
                     from_zones=("local",), to_zones=("app",), name="I")
    broad = _change(srcs=[("sb", "ip-netmask", "10.0.0.0/8")], dsts=[HOST], services=[HTTPS],
                    from_zones=("local",), to_zones=("internet",), name="B")  # different to-zone
    policy = PolicyContext.from_changes([inside, broad])
    assert "shadowed_by" not in _fired(classify(inside, policy=policy))  # zones not a superset
