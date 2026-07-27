"""Tests for the intent schema + fail-closed loader (Day-2, Phase 1)."""

from __future__ import annotations

import copy
from datetime import date

import pytest

from fwgitops.intent import (
    AccessRequest,
    Endpoint,
    IntentError,
    Service,
    load_intent,
)


def valid_doc() -> dict:
    return {
        "apiVersion": "fw-intent/v1",
        "kind": "AccessRequest",
        "metadata": {
            "id": "REQ-2026-0417",
            "requester": "jane.doe@corp",
            "ticket": "JIRA-12345",
            "justification": "Web tier needs to reach the payments API",
            "requested": "2026-07-19",
            "expires": "2026-10-19",
        },
        "spec": {
            "environment": "prod",
            "action": "allow",
            "source": [{"cidr": "10.20.1.0/24"}],
            "destination": [{"fqdn": "payments.internal"}, {"cidr": "10.20.9.10/32"}],
            "service": [{"protocol": "tcp", "port": "443"}, {"protocol": "tcp", "port": "8000-8100"}],
            "log": True,
        },
    }


def problems(doc: dict) -> list[str]:
    with pytest.raises(IntentError) as ei:
        load_intent(doc)
    return [p.path for p in ei.value.problems]


# ── Happy path ────────────────────────────────────────────────────────────
def test_valid_intent_parses():
    ar = load_intent(valid_doc())
    assert isinstance(ar, AccessRequest)
    assert ar.metadata.id == "REQ-2026-0417"
    assert ar.metadata.expires == date(2026, 10, 19)
    assert ar.spec.action == "allow"
    assert ar.spec.source == [Endpoint("cidr", "10.20.1.0/24")]
    assert Endpoint("fqdn", "payments.internal") in ar.spec.destination
    assert Service("tcp", "443") in ar.spec.service
    assert ar.spec.log is True


def test_log_defaults_true_and_expires_optional():
    doc = valid_doc()
    del doc["spec"]["log"]
    del doc["metadata"]["expires"]
    ar = load_intent(doc)
    assert ar.spec.log is True
    assert ar.metadata.expires is None


# ── Envelope ──────────────────────────────────────────────────────────────
def test_wrong_api_version_and_kind():
    doc = valid_doc()
    doc["apiVersion"] = "v0"
    doc["kind"] = "Nope"
    assert set(problems(doc)) >= {"apiVersion", "kind"}


def test_unknown_kind_lists_supported_and_stops(capsys=None):
    # ADR-0001: an unknown kind can't have its spec validated (no schema), so it
    # reports the envelope and stops — with an actionable "supported" list.
    doc = valid_doc()
    doc["kind"] = "NatRequest"         # not registered
    del doc["metadata"]["ticket"]      # a spec problem that must NOT be reported
    with pytest.raises(IntentError) as ei:
        load_intent(doc)
    paths = {p.path for p in ei.value.problems}
    assert paths == {"kind"}           # only the kind problem — spec not validated
    msg = next(p for p in ei.value.problems if p.path == "kind").message
    assert "unsupported kind 'NatRequest'" in msg and "AccessRequest" in msg and "ZoneRequest" in msg


# ── ZoneRequest (kind #2) ──────────────────────────────────────────────────
def _zone_doc():
    return {
        "apiVersion": "fw-intent/v1", "kind": "ZoneRequest",
        "metadata": {"id": "ZONE-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "dmz for partner", "requested": "2026-07-27"},
        "spec": {"environment": "prod", "zone": "dmz", "type": "layer3",
                 "interfaces": ["ethernet1/2"]},
    }


def test_zone_request_loads():
    from fwgitops.intent import ZoneRequest
    zr = load_intent(_zone_doc())
    assert isinstance(zr, ZoneRequest)
    assert zr.spec.environment == "prod" and zr.spec.zone == "dmz"
    assert zr.spec.zone_type == "layer3" and zr.spec.interfaces == ["ethernet1/2"]


def test_zone_request_empty_interfaces_ok():
    doc = _zone_doc()
    doc["spec"]["interfaces"] = []
    assert load_intent(doc).spec.interfaces == []  # empty typed zone is valid


def test_zone_request_bad_type_rejected():
    doc = _zone_doc()
    doc["spec"]["type"] = "layer9"
    with pytest.raises(IntentError, match="spec.type"):
        load_intent(doc)


def test_zone_request_missing_zone_rejected():
    doc = _zone_doc()
    del doc["spec"]["zone"]
    with pytest.raises(IntentError, match="spec.zone"):
        load_intent(doc)


def test_zone_request_bad_interface_rejected():
    doc = _zone_doc()
    doc["spec"]["interfaces"] = [123]  # not a string
    with pytest.raises(IntentError, match="spec.interfaces"):
        load_intent(doc)


def test_non_mapping_document():
    with pytest.raises(IntentError):
        load_intent(["not", "a", "mapping"])


# ── Metadata ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("missing", ["id", "requester", "ticket", "justification", "requested"])
def test_required_metadata_fields(missing):
    doc = valid_doc()
    del doc["metadata"][missing]
    assert f"metadata.{missing}" in problems(doc)


def test_ticket_is_mandatory_audit_linkage():
    doc = valid_doc()
    del doc["metadata"]["ticket"]
    assert "metadata.ticket" in problems(doc)


@pytest.mark.parametrize("field", ["id", "ticket"])
def test_unsafe_tag_value_rejected(field):
    doc = valid_doc()
    doc["metadata"][field] = "has:colon"
    assert f"metadata.{field}" in problems(doc)


def test_bad_date_rejected():
    doc = valid_doc()
    doc["metadata"]["requested"] = "19-07-2026"
    assert "metadata.requested" in problems(doc)


# ── Spec ──────────────────────────────────────────────────────────────────
def test_invalid_action():
    doc = valid_doc()
    doc["spec"]["action"] = "permit"
    assert "spec.action" in problems(doc)


def test_missing_environment():
    doc = valid_doc()
    del doc["spec"]["environment"]
    assert "spec.environment" in problems(doc)


def test_empty_source_rejected():
    doc = valid_doc()
    doc["spec"]["source"] = []
    assert "spec.source" in problems(doc)


def test_endpoint_needs_exactly_one_key():
    doc = valid_doc()
    doc["spec"]["source"] = [{"cidr": "10.0.0.0/8", "fqdn": "x.y"}]
    assert "spec.source[0]" in problems(doc)


def _app_catalog():
    from fwgitops.catalog import AppCatalog
    return AppCatalog.from_dict({"apps": {
        "web-tier": {"environment": "prod", "folder": "prod-edge", "zone": "local",
                     "addresses": ["10.20.1.0/24"]},
        "payments": {"environment": "prod", "folder": "prod-edge", "zone": "app",
                     "addresses": ["10.20.9.10/32"], "fqdns": ["pay.internal"]},
        "staging-x": {"environment": "staging", "folder": "stg", "zone": "local",
                      "addresses": ["10.99.0.0/24"]},
    }})


def test_app_endpoint_expands_with_zone():
    doc = valid_doc()
    doc["spec"]["source"] = [{"app": "payments"}]  # 1 cidr + 1 fqdn, zone "app"
    ar = load_intent(doc, app_catalog=_app_catalog())
    kinds = {(e.kind, e.value, e.zone) for e in ar.spec.source}
    assert kinds == {("cidr", "10.20.9.10/32", "app"), ("fqdn", "pay.internal", "app")}


def test_app_endpoint_without_catalog_is_rejected():
    doc = valid_doc()
    doc["spec"]["source"] = [{"app": "web-tier"}]
    with pytest.raises(IntentError) as ei:
        load_intent(doc)  # no catalog
    prob = next(p for p in ei.value.problems if p.path == "spec.source[0].app")
    assert "no app catalog" in prob.message and "cidr" in prob.message


def test_unknown_app_is_rejected():
    doc = valid_doc()
    doc["spec"]["source"] = [{"app": "ghost"}]
    with pytest.raises(IntentError, match="unknown app"):
        load_intent(doc, app_catalog=_app_catalog())


def test_app_environment_mismatch_is_rejected():
    doc = valid_doc()  # environment: prod
    doc["spec"]["source"] = [{"app": "staging-x"}]  # app is in 'staging'
    with pytest.raises(IntentError, match="environment"):
        load_intent(doc, app_catalog=_app_catalog())


def test_cidr_host_bits_hint():
    doc = valid_doc()
    doc["spec"]["source"] = [{"cidr": "10.20.1.5/24"}]  # host bits set
    with pytest.raises(IntentError) as ei:
        load_intent(doc)
    prob = next(p for p in ei.value.problems if p.path == "spec.source[0].cidr")
    assert "host bits" in prob.message


def test_invalid_fqdn():
    doc = valid_doc()
    doc["spec"]["destination"] = [{"fqdn": "not a domain"}]
    assert "spec.destination[0].fqdn" in problems(doc)


# ── Service ───────────────────────────────────────────────────────────────
def test_service_name_resolves_with_catalog():
    from fwgitops.catalog import ServiceCatalog
    doc = valid_doc()
    doc["spec"]["service"] = [{"name": "https"}]
    cat = ServiceCatalog.from_dict({"services": {"https": {"protocol": "tcp", "port": "443"}}})
    ar = load_intent(doc, service_catalog=cat)
    assert ar.spec.service[0].protocol == "tcp" and ar.spec.service[0].port == "443"


def test_service_name_without_catalog_is_rejected():
    doc = valid_doc()
    doc["spec"]["service"] = [{"name": "https"}]
    with pytest.raises(IntentError) as ei:
        load_intent(doc)  # no catalog
    prob = next(p for p in ei.value.problems if p.path == "spec.service[0].name")
    assert "no service catalog" in prob.message and "protocol" in prob.message


def test_unknown_service_name_is_rejected():
    from fwgitops.catalog import ServiceCatalog
    doc = valid_doc()
    doc["spec"]["service"] = [{"name": "ftp"}]
    cat = ServiceCatalog.from_dict({"services": {"https": {"protocol": "tcp", "port": "443"}}})
    with pytest.raises(IntentError, match="unknown service"):
        load_intent(doc, service_catalog=cat)


def test_invalid_protocol():
    doc = valid_doc()
    doc["spec"]["service"] = [{"protocol": "icmp", "port": "0"}]
    assert "spec.service[0].protocol" in problems(doc)


@pytest.mark.parametrize("port,expected", [
    ("70000", True),      # out of range
    ("8100-8000", True),  # descending
    ("abc", True),        # non-numeric
    ("443", False),       # ok
    ("8000-8100", False), # ok range
])
def test_port_validation(port, expected):
    doc = valid_doc()
    doc["spec"]["service"] = [{"protocol": "tcp", "port": port}]
    if expected:
        assert "spec.service[0].port" in problems(doc)
    else:
        load_intent(doc)  # should not raise


# ── Multi-problem collection ──────────────────────────────────────────────
def test_all_problems_collected_not_just_first():
    doc = valid_doc()
    doc["apiVersion"] = "v0"
    del doc["metadata"]["ticket"]
    doc["spec"]["action"] = "permit"
    doc["spec"]["source"] = []
    paths = problems(doc)
    # Envelope, metadata, and spec problems all surface in one IntentError.
    assert {"apiVersion", "metadata.ticket", "spec.action", "spec.source"} <= set(paths)
