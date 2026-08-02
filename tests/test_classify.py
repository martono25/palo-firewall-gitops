"""Tests for the Phase-2 risk classifier (policy-as-code, fail-closed)."""

from __future__ import annotations

import pytest

from fwgitops.classify import CLASSIFIER_VERSION, DEFAULT_THRESHOLDS, Thresholds, classify
from fwgitops.compiler import AddressObject, CompiledChange, SecurityRule, ServiceObject


def _change(*, srcs, dsts, services, from_zones=("local",), to_zones=("internet",),
            action="allow", name="R", folder="f", profile_group="inspect-grp",
            negate_source=False, negate_destination=False):
    """Build a CompiledChange.

    srcs/dsts: list of (name, type, value); services: list of (name, proto, port).
    A profile group is attached by default so the inspection-posture check does
    not fire in tests focused on other risk dimensions; pass profile_group=None
    to exercise allow_without_inspection.
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
        action=action, log_end=True, tags=[], profile_group=profile_group,
        negate_source=negate_source, negate_destination=negate_destination,
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


# ── Negation (v1.0) — fail closed ─────────────────────────────────────────
def test_negated_source_escalates_high():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                         services=[HTTPS], negate_source=True))
    assert v.tier == "HIGH" and "negated_match" in _fired(v)


def test_negated_destination_escalates_high():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                         services=[HTTPS], negate_destination=True))
    assert "negated_match" in _fired(v)


def test_no_negation_does_not_fire():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST], services=[HTTPS]))
    assert "negated_match" not in _fired(v)


# ── Inspection posture (ADR-0003) ─────────────────────────────────────────
def test_allow_without_profile_fires_low():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                         services=[HTTPS], profile_group=None))
    assert "allow_without_inspection" in _fired(v)
    assert v.tier == "LOW"  # surfaced for evidence, not a gate escalation


def test_allow_with_profile_is_clean():
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                         services=[HTTPS], profile_group="inspect-grp"))
    assert "allow_without_inspection" not in _fired(v)
    assert v.checks_fired == ()


def test_deny_without_profile_does_not_fire():
    # A deny performs no inspection by definition; the check is allow-only.
    v = classify(_change(srcs=[("s", "ip-netmask", "10.20.1.0/24")], dsts=[HOST],
                         services=[HTTPS], action="deny", profile_group=None))
    assert "allow_without_inspection" not in _fired(v)


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


# ── ZoneRequest classification (ADR-0001 kind #2) ──────────────────────────
def _zone(**kw):
    from fwgitops.compiler import CompiledZone
    base = dict(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=[])
    base.update(kw)
    return CompiledZone(**base)


def test_zone_without_a_protection_profile_is_high():
    """The ADR-0003 `allow_without_inspection` lesson, applied to zones: the
    ABSENCE of a security control is a finding, not a default."""
    from fwgitops.classify import classify_zone
    v = classify_zone(_zone())
    assert v.tier == "HIGH"
    assert "zone_without_protection" in [c["check"] for c in v.checks_fired]


def test_user_id_off_is_flagged_because_source_user_rules_silently_never_match():
    from fwgitops.classify import classify_zone
    checks = [c["check"] for c in classify_zone(_zone(protection_profile="best-practice")).checks_fired]
    assert checks == ["user_id_disabled_on_zone"]


def test_a_fully_configured_zone_is_low_with_no_findings():
    from fwgitops.classify import classify_zone
    v = classify_zone(_zone(protection_profile="best-practice", user_id=True))
    assert v.tier == "LOW" and v.checks_fired == ()


def test_zone_verdict_carries_classifier_provenance():
    """The verdict feeds the evidence bundle, so it must be attributable."""
    from fwgitops.classify import classify_zone
    v = classify_zone(_zone())
    assert v.classifier_version and v.thresholds_version


# ── folder blast radius (ADR-0005 prerequisite #1) ─────────────────────────
def _hierarchy(**kw):
    from fwgitops.catalog import FolderHierarchy
    return FolderHierarchy.from_dict({"folders": kw})


def test_a_change_scoped_to_a_folder_with_children_is_high():
    """One change at `ngfw-shared` lands on production AND the sandbox. The
    largest blast radius this platform can produce, so it must not auto-apply."""
    from fwgitops.classify import classify_zone
    h = _hierarchy(**{"ngfw-shared": {"children": ["prod-edge", "GitOps"]}})
    v = classify_zone(_zone(folder="ngfw-shared", protection_profile="p", user_id=True),
                      hierarchy=h)
    assert v.tier == "HIGH"
    fired = [c for c in v.checks_fired if c["check"] == "folder_with_children"]
    assert fired and "prod-edge" in fired[0]["reason"] and "GitOps" in fired[0]["reason"]


def test_a_leaf_folder_is_not_tiered_up():
    from fwgitops.classify import classify_zone
    h = _hierarchy(**{"ngfw-shared": {"children": ["prod-edge"]}, "prod-edge": {"children": []}})
    v = classify_zone(_zone(folder="prod-edge", protection_profile="p", user_id=True), hierarchy=h)
    assert v.tier == "LOW" and v.checks_fired == ()


def test_no_hierarchy_configured_means_no_check_not_a_silent_pass():
    """Absent hierarchy disables the check; it must not invent a verdict."""
    from fwgitops.classify import classify_zone
    v = classify_zone(_zone(folder="ngfw-shared", protection_profile="p", user_id=True))
    assert [c["check"] for c in v.checks_fired] == []


def test_the_blast_radius_check_applies_to_rules_too():
    """Not zone-specific: an env map pointing at a parent folder puts RULES
    there, with the same reach."""
    from fwgitops.classify import classify
    h = _hierarchy(**{"ngfw-shared": {"children": ["prod-edge", "GitOps"]}})
    ch = _change(srcs=[("a", "ip-netmask", "10.0.0.1/32")],
                 dsts=[("b", "ip-netmask", "10.0.1.1/32")],
                 services=[("https", "tcp", "443")],
                 folder="ngfw-shared")
    v = classify(ch, hierarchy=h)
    assert "folder_with_children" in [c["check"] for c in v.checks_fired]
    assert v.tier == "HIGH"


def test_the_shipped_folder_hierarchy_parses_and_matches_the_tenant():
    import yaml

    from fwgitops.catalog import FolderHierarchy
    h = FolderHierarchy.from_dict(yaml.safe_load(open("catalog/folders.yaml")))
    # Verified live 2026-08-02, CONTAINERS only:
    #   All -> ngfw-shared -> {prod-edge, GitOps}
    assert h.has_children("ngfw-shared")
    assert h.children_of("ngfw-shared") == frozenset({"prod-edge", "GitOps"})
    assert not h.has_children("prod-edge")
    assert not h.has_children("GitOps")


def test_device_entries_are_not_folders_and_are_absent_from_the_catalog():
    """Regression, and a correction of one.

    v1.11.0 read GET /config/setup/v1/folders, saw two entries parented to
    `prod-edge` named for device serials, and listed them as child folders —
    marked targetable. They are not folders. The listing mixes two entry kinds,
    told apart by `type`: `container` is a real folder, `on-prem` is a DEVICE
    (it carries `serial_number` and `model`).

    Confirmed three ways: `folder=<serial>` returns 400 "Folder doesn't exist";
    the same serial works as `device=`; and pan.dev plus the Terraform provider
    both treat folder / snippet / device as three separate scopes ("exactly one
    of"). An intent naming one would have compiled clean and failed at apply.
    """
    import yaml

    from fwgitops.catalog import FolderHierarchy
    h = FolderHierarchy.from_dict(yaml.safe_load(open("catalog/folders.yaml")))
    for serial in ("007955000894453", "007955000893662"):
        assert not h.known(serial), f"{serial} is a device, not a folder"
        assert not h.is_targetable(serial)
    assert h.targetable_folders() == ["GitOps", "prod-edge"]


def test_shared_parents_are_not_targetable_but_leaves_are():
    """`folder:` in a Day-1 intent is only safe because of this. Targetability
    is checked at COMPILE time rather than tiered up, because HIGH is approvable
    and a write to a shared parent should not be one rubber-stamp away."""
    import yaml

    from fwgitops.catalog import FolderHierarchy
    h = FolderHierarchy.from_dict(yaml.safe_load(open("catalog/folders.yaml")))
    assert not h.is_targetable("ngfw-shared")     # parents prod AND sandbox
    assert h.is_targetable("prod-edge")
    assert h.is_targetable("GitOps")
    # Fail closed: never seen == never targetable.
    assert not h.is_targetable("All")
    assert not h.is_targetable("typo-folder")
    assert not h.known("typo-folder")


def test_a_device_folder_name_must_be_quoted_in_yaml():
    """An unquoted serial with no leading zero parses as an int. Coercing it
    back to a string is worse than rejecting — it would never match the real
    folder — so reject with an actionable message."""
    import yaml

    from fwgitops.catalog import CatalogError, FolderHierarchy
    with pytest.raises(CatalogError, match="quote it"):
        FolderHierarchy.from_dict(
            yaml.safe_load("folders:\n  123456789012345:\n    children: []\n"))


def test_the_tenants_serials_survive_unquoted_only_by_luck():
    """Both current serials start with `00`, which YAML rejects as octal and
    falls back to str — so they parse correctly even unquoted. That is luck, not
    design, and the reason the shipped catalog quotes them anyway."""
    import yaml

    parsed = yaml.safe_load("folders:\n  007955000894453:\n    children: []\n")
    assert isinstance(next(iter(parsed["folders"])), str)


@pytest.mark.parametrize("bad", [
    {"folders": {"a": {"children": "not-a-list"}}},
    {"folders": {"a": {"children": [""]}}},
    {"folders": "nope"},
    "nope",
])
def test_bad_folder_hierarchy_shapes_fail_closed(bad):
    from fwgitops.catalog import CatalogError, FolderHierarchy
    with pytest.raises(CatalogError):
        FolderHierarchy.from_dict(bad)


# ── novel population (ADR-0005 prerequisite #2) ────────────────────────────
def _cur(name="dmz", folder="prod-edge", ifaces=()):
    return {(folder, name): {"name": name, "network": {"layer3": list(ifaces)}}}


def test_a_zone_gaining_its_first_interface_is_high():
    """Populating a previously-empty field is a different act from editing one.
    Four of the seven zones on the pilot tenant sit at `layer3: []`, so this is
    the normal state — moving out of it changes what the firewall passes."""
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=["$eth-local"], protection_profile="p", user_id=True)
    v = classify_zone(z, current=_cur(ifaces=[]))
    assert v.tier == "HIGH"
    fired = [c for c in v.checks_fired if c["check"] == "zone_becomes_traffic_bearing"]
    assert fired and "$eth-local" in fired[0]["reason"]


def test_changing_the_interfaces_of_a_live_zone_is_not_the_same_act():
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=["$eth-local"], protection_profile="p", user_id=True)
    v = classify_zone(z, current=_cur(ifaces=["$eth-other"]))
    assert v.tier == "LOW" and v.checks_fired == ()


def test_a_zone_that_does_not_exist_yet_is_not_flagged_by_this_check():
    """Creation is covered elsewhere; this check is about CHANGING something
    already present."""
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=["$eth-local"], protection_profile="p", user_id=True)
    assert classify_zone(z, current={}).checks_fired == ()


def test_without_a_snapshot_the_check_is_skipped_not_guessed():
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=["$eth-local"], protection_profile="p", user_id=True)
    assert classify_zone(z).checks_fired == ()


def test_declaring_no_interfaces_never_triggers_it():
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=[], protection_profile="p", user_id=True)
    assert classify_zone(z, current=_cur(ifaces=[])).checks_fired == ()


def test_interfaces_are_counted_across_every_layer_type():
    """A zone is traffic-bearing whether its interfaces are layer3, layer2, tap…"""
    from fwgitops.classify import classify_zone
    z = _zone(interfaces=["$eth-local"], protection_profile="p", user_id=True)
    cur = {("prod-edge", "dmz"): {"network": {"layer2": ["$eth-x"], "layer3": []}}}
    assert classify_zone(z, current=cur).checks_fired == ()
