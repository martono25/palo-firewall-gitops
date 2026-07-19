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
