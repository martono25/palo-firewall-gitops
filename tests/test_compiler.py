"""Tests for Phase-1 resolve + compiler (intent → tfvars)."""

from __future__ import annotations

import copy
import json

import pytest

from fwgitops.compiler import (
    CompileError,
    compile_request,
    dumps_tfvars,
    to_tfvars,
)
from fwgitops.intent import load_intent
from fwgitops.resolve import EnvMap, ResolveError
from fwgitops.tags import MANAGED_TAG, is_managed, parse_managed_meta

from test_intent import valid_doc


def env_map() -> EnvMap:
    return EnvMap.from_dict(
        {"prod": {"folder": "prod-edge", "from_zone": "trust", "to_zone": "app"}}
    )


def compiled(doc=None):
    ar = load_intent(doc or valid_doc())
    return compile_request(ar, env_map())


# ── resolve ───────────────────────────────────────────────────────────────
def test_resolve_known_env():
    r = env_map().resolve("prod")
    assert (r.folder, r.from_zone, r.to_zone) == ("prod-edge", "trust", "app")


def test_resolve_unknown_env_lists_known():
    with pytest.raises(ResolveError) as ei:
        env_map().resolve("staging")
    assert "prod" in str(ei.value)


@pytest.mark.parametrize("bad", [{}, {"prod": {"folder": "x"}}, "nope"])
def test_env_map_from_dict_fails_closed(bad):
    with pytest.raises(ResolveError):
        EnvMap.from_dict(bad)


# ── compile_request ───────────────────────────────────────────────────────
def test_rule_identity_and_zones():
    ch = compiled()
    assert ch.rule.name == "REQ-2026-0417"
    assert ch.rule.folder == "prod-edge"
    assert ch.rule.from_zones == ["trust"]
    assert ch.rule.to_zones == ["app"]
    assert ch.rule.action == "allow"
    assert ch.rule.log_end is True


# ── ADR-0003 rule components → SecurityRule + tfvars ───────────────────────
def test_rule_component_defaults_compiled():
    r = compiled().rule
    assert r.application == ["any"]
    assert r.profile_group is None
    assert r.log_setting is None
    assert r.rulebase == "pre"
    assert r.relative_position == "bottom"
    assert r.target_rule is None


def test_rule_components_explicit_compiled():
    from test_intent import valid_doc as _vd
    doc = _vd()
    doc["spec"].update({
        "application": ["ssl"], "profile": "strict",
        "log_forwarding": "siem", "position": "after:REQ-9",
    })
    r = compile_request(load_intent(doc), env_map()).rule
    assert r.application == ["ssl"] and r.profile_group == "strict"
    assert r.log_setting == "siem"
    assert r.relative_position == "after" and r.target_rule == "REQ-9"


def test_v1_fields_compiled():
    from test_intent import valid_doc as _vd
    doc = _vd()
    doc["spec"].update({
        "action": "drop", "description": "blocklist", "log_start": True,
        "source_user": ["corp\\bob"], "category": ["gambling"], "negate_source": True,
    })
    r = compile_request(load_intent(doc), env_map()).rule
    assert r.action == "drop" and r.description == "blocklist" and r.log_start is True
    assert r.source_user == ["corp\\bob"] and r.category == ["gambling"]
    assert r.negate_source is True and r.negate_destination is False


def test_position_top_has_no_target():
    doc = valid_doc()
    doc["spec"]["position"] = "top"
    r = compile_request(load_intent(doc), env_map()).rule
    assert r.relative_position == "top" and r.target_rule is None


def test_tfvars_carries_rule_components():
    rule = to_tfvars([compiled()])["security_rules"]["REQ-2026-0417"]
    for k in ("application", "profile_group", "log_setting",
              "rulebase", "relative_position", "target_rule"):
        assert k in rule
    assert rule["application"] == ["any"]
    assert rule["rulebase"] == "pre"


def _app_catalog():
    from fwgitops.catalog import AppCatalog
    return AppCatalog.from_dict({"apps": {
        "web": {"environment": "prod", "folder": "prod-edge", "zone": "dmz",
                "addresses": ["10.20.1.0/24"]},
        "pay": {"environment": "prod", "folder": "prod-edge", "zone": "app",
                "addresses": ["10.20.9.10/32"]},
    }})


def test_zones_derived_from_apps_not_env_default():
    doc = valid_doc()
    doc["spec"]["source"] = [{"app": "web"}]        # zone dmz
    doc["spec"]["destination"] = [{"app": "pay"}]   # zone app
    ch = compile_request(load_intent(doc, app_catalog=_app_catalog()), env_map())
    assert ch.rule.from_zones == ["dmz"]   # from the source app, NOT env default "trust"
    assert ch.rule.to_zones == ["app"]


def test_explicit_endpoint_uses_env_default_zone():
    doc = valid_doc()
    doc["spec"]["source"] = [{"cidr": "10.20.1.0/24"}]   # explicit -> env default
    doc["spec"]["destination"] = [{"app": "pay"}]        # app zone
    ch = compile_request(load_intent(doc, app_catalog=_app_catalog()), env_map())
    assert ch.rule.from_zones == ["trust"]   # env default (explicit source)
    assert ch.rule.to_zones == ["app"]


# ── ZoneRequest (kind #2) compile ──────────────────────────────────────────
def _zone_doc():
    return {
        "apiVersion": "fw-intent/v1", "kind": "ZoneRequest",
        "metadata": {"id": "ZONE-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "dmz", "requested": "2026-07-27"},
        "spec": {"environment": "prod", "zone": "dmz", "type": "layer3",
                 "interfaces": ["ethernet1/2"]},
    }


def test_compile_any_dispatches_zone_request():
    from fwgitops.compiler import CompiledZone, compile_any
    cz = compile_any(load_intent(_zone_doc()), env_map())
    assert isinstance(cz, CompiledZone)
    assert cz.folder == "prod-edge" and cz.name == "dmz"
    assert cz.zone_type == "layer3" and cz.interfaces == ["ethernet1/2"]


def test_zone_tfvars_shape():
    from fwgitops.compiler import CompiledZone, zone_tfvars
    z = CompiledZone(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=["e1/2"])
    assert zone_tfvars([z]) == {
        "zones": {"dmz": {"name": "dmz", "folder": "prod-edge", "network": {"layer3": ["e1/2"]}}}
    }


def test_compile_any_unknown_request_type_raises():
    from fwgitops.compiler import CompileError, compile_any
    with pytest.raises(CompileError, match="no compiler"):
        compile_any(object(), env_map())


# ── Cross-kind zone consistency ────────────────────────────────────────────
def _rule_using_dmz():
    # web (zone dmz) -> pay (zone app), via the app catalog
    doc = valid_doc()
    doc["spec"]["source"] = [{"app": "web"}]        # zone dmz
    doc["spec"]["destination"] = [{"app": "pay"}]   # zone app
    return compile_request(load_intent(doc, app_catalog=_app_catalog()), env_map())


def test_zone_consistency_ok_for_baseline_zones():
    from fwgitops.compiler import check_zone_consistency
    assert check_zone_consistency([compiled()], [], env_map()) == []  # trust->app are baseline


def test_zone_consistency_flags_undeclared_zone():
    from fwgitops.compiler import check_zone_consistency
    v = check_zone_consistency([_rule_using_dmz()], [], env_map())  # dmz undeclared
    assert len(v) == 1 and "dmz" in v[0] and "ZoneRequest" in v[0]


def test_zone_request_makes_zone_declared():
    from fwgitops.compiler import CompiledZone, check_zone_consistency
    zones = [CompiledZone(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=[])]
    assert check_zone_consistency([_rule_using_dmz()], zones, env_map()) == []  # now declared


def test_rule_carries_full_provenance_tags():
    ch = compiled()
    assert is_managed(ch.rule.tags)
    meta = parse_managed_meta(ch.rule.tags)
    assert meta.req_id == "REQ-2026-0417"
    assert meta.ticket == "JIRA-12345"
    assert meta.section.value == "specific-allow"
    assert meta.expires is not None


def test_objects_tagged_managed_marker_only():
    ch = compiled()
    for obj in [*ch.address_objects, *ch.service_objects]:
        assert obj.tags == [MANAGED_TAG]  # provenance lives on the rule, not objects


def test_address_object_types():
    ch = compiled()
    by_value = {a.value: a.type for a in ch.address_objects}
    assert by_value["10.20.1.0/24"] == "ip-netmask"
    assert by_value["payments.internal"] == "fqdn"
    assert by_value["10.20.9.10/32"] == "ip-netmask"


def test_rule_references_resolve_to_objects():
    ch = compiled()
    names = {a.name for a in ch.address_objects} | {s.name for s in ch.service_objects}
    for ref in [*ch.rule.sources, *ch.rule.destinations, *ch.rule.services]:
        assert ref in names


def test_dedup_shared_cidr_within_request():
    doc = valid_doc()
    doc["spec"]["source"] = [{"cidr": "10.20.9.10/32"}]
    doc["spec"]["destination"] = [{"cidr": "10.20.9.10/32"}]  # same value both sides
    ch = compiled(doc)
    # One address object, referenced by both source and destination.
    assert len(ch.address_objects) == 1
    only = ch.address_objects[0].name
    assert ch.rule.sources == [only] and ch.rule.destinations == [only]


def test_unknown_environment_fails_closed():
    doc = valid_doc()
    doc["spec"]["environment"] = "dev"  # not in the env map
    with pytest.raises(ResolveError):
        compiled(doc)


# ── to_tfvars / aggregation ───────────────────────────────────────────────
def test_tfvars_structure():
    tf = to_tfvars([compiled()])
    assert set(tf) == {"address_objects", "service_objects", "security_rules"}
    assert "REQ-2026-0417" in tf["security_rules"]


def test_object_dedup_across_changes():
    # Two requests referencing the same CIDR → one address object in the aggregate.
    doc2 = valid_doc()
    doc2["metadata"]["id"] = "REQ-2026-0500"
    doc2["spec"]["destination"] = [{"cidr": "10.20.9.10/32"}]
    doc2["spec"]["service"] = [{"protocol": "tcp", "port": "443"}]
    tf = to_tfvars([compiled(), compiled(doc2)])
    # 10.20.9.10/32 appears in both → single object; two rules.
    assert len(tf["security_rules"]) == 2
    values = {o["value"] for o in tf["address_objects"].values()}
    assert "10.20.9.10/32" in values


def test_duplicate_rule_key_raises():
    with pytest.raises(CompileError):
        to_tfvars([compiled(), compiled()])  # same metadata.id twice


# ── determinism ───────────────────────────────────────────────────────────
def test_dumps_is_byte_stable():
    a = dumps_tfvars([compiled()])
    b = dumps_tfvars([compiled()])
    assert a == b
    json.loads(a)  # valid JSON


def test_change_order_does_not_affect_output():
    doc2 = valid_doc()
    doc2["metadata"]["id"] = "REQ-2026-0500"
    c1, c2 = compiled(), compiled(doc2)
    assert dumps_tfvars([c1, c2]) == dumps_tfvars([c2, c1])
