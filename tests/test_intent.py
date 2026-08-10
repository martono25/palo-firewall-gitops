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


# ── ADR-0003 rule components (optional; default = plain L4 allow) ──────────
def test_rule_components_default_when_omitted():
    sp = load_intent(valid_doc()).spec
    assert sp.application == ["any"]
    assert sp.profile is None
    assert sp.log_forwarding is None
    # UNSPECIFIED, not "bottom". The two used to be the same value, which is what
    # blocked wiring ordering into Terraform: writing a concrete position MOVES
    # the rule, so a default that reads as a request reorders policy silently.
    assert sp.position is None


def test_rule_components_explicit():
    doc = valid_doc()
    doc["spec"].update({
        "application": ["ssl", "web-browsing"],
        "profile": "strict-inspection",
        "log_forwarding": "siem-forward",
        "position": "before:REQ-2026-0001",
    })
    sp = load_intent(doc).spec
    assert sp.application == ["ssl", "web-browsing"]
    assert sp.profile == "strict-inspection"
    assert sp.log_forwarding == "siem-forward"
    assert sp.position == "before:REQ-2026-0001"


@pytest.mark.parametrize("bad,path", [
    ({"application": []}, "spec.application"),
    ({"application": ["", "x"]}, "spec.application"),
    ({"application": "ssl"}, "spec.application"),
    ({"profile": ""}, "spec.profile"),
    ({"log_forwarding": "  "}, "spec.log_forwarding"),
    ({"position": "middle"}, "spec.position"),
    ({"position": "before:"}, "spec.position"),
    ({"position": "before"}, "spec.position"),
])
def test_rule_components_fail_closed(bad, path):
    doc = valid_doc()
    doc["spec"].update(bad)
    assert path in problems(doc)


# ── v1.0 rule completeness fields ──────────────────────────────────────────
def test_v1_fields_default_when_omitted():
    sp = load_intent(valid_doc()).spec
    assert sp.description is None
    assert sp.log_start is False
    assert sp.source_user == ["any"]
    assert sp.category == ["any"]
    assert sp.negate_source is False and sp.negate_destination is False


def test_v1_fields_explicit():
    doc = valid_doc()
    doc["spec"].update({
        "description": "web tier to payments",
        "log_start": True,
        "source_user": ["corp\\alice", "grp-payments"],
        "category": ["financial-services"],
        "negate_source": True,
        "negate_destination": False,
    })
    sp = load_intent(doc).spec
    assert sp.description == "web tier to payments"
    assert sp.log_start is True
    assert sp.source_user == ["corp\\alice", "grp-payments"]
    assert sp.category == ["financial-services"]
    assert sp.negate_source is True


@pytest.mark.parametrize("action", ["drop", "reset-client", "reset-server", "reset-both"])
def test_extended_actions_accepted(action):
    doc = valid_doc()
    doc["spec"]["action"] = action
    assert load_intent(doc).spec.action == action


def test_unknown_action_rejected():
    doc = valid_doc()
    doc["spec"]["action"] = "bounce"
    assert "spec.action" in problems(doc)


@pytest.mark.parametrize("bad,path", [
    ({"log_start": "yes"}, "spec.log_start"),
    ({"negate_source": 1}, "spec.negate_source"),
    ({"source_user": []}, "spec.source_user"),
    ({"source_user": "alice"}, "spec.source_user"),
    ({"category": [""]}, "spec.category"),
    ({"description": "  "}, "spec.description"),
])
def test_v1_fields_fail_closed(bad, path):
    doc = valid_doc()
    doc["spec"].update(bad)
    assert path in problems(doc)


# ── Catalog-backed name validation (ADR-0003) ─────────────────────────────
from fwgitops.catalog import NameCatalog  # noqa: E402


def _app_cat(names=("ssl", "web-browsing")):
    return NameCatalog.from_dict(list(names), kind="App-ID", key="applications",
                                 always_valid=frozenset({"any"}))


def _profile_cat(names=("strict", "default")):
    return NameCatalog.from_dict(list(names), kind="security profile group", key="profiles")


def test_application_validated_against_catalog():
    doc = valid_doc()
    doc["spec"]["application"] = ["ssl"]
    ar = load_intent(doc, application_catalog=_app_cat())
    assert ar.spec.application == ["ssl"]


def test_unknown_application_rejected():
    doc = valid_doc()
    doc["spec"]["application"] = ["ssl", "bogus-app"]
    with pytest.raises(IntentError) as ei:
        load_intent(doc, application_catalog=_app_cat())
    paths = [p.path for p in ei.value.problems]
    assert "spec.application" in paths
    assert any("bogus-app" in p.message for p in ei.value.problems)


def test_application_any_always_valid_even_with_catalog():
    # default application is ["any"]; must pass without being listed
    load_intent(valid_doc(), application_catalog=_app_cat())


def test_no_application_catalog_accepts_free_form():
    doc = valid_doc()
    doc["spec"]["application"] = ["anything-goes"]
    assert load_intent(doc).spec.application == ["anything-goes"]


def test_profile_validated_against_catalog():
    doc = valid_doc()
    doc["spec"]["profile"] = "strict"
    assert load_intent(doc, profile_catalog=_profile_cat()).spec.profile == "strict"


def test_unknown_profile_rejected():
    doc = valid_doc()
    doc["spec"]["profile"] = "loose"
    with pytest.raises(IntentError) as ei:
        load_intent(doc, profile_catalog=_profile_cat())
    assert "spec.profile" in [p.path for p in ei.value.problems]


def test_unknown_log_forwarding_rejected():
    doc = valid_doc()
    doc["spec"]["log_forwarding"] = "nowhere"
    lf = NameCatalog.from_dict(["siem-forward"], kind="log-forwarding profile", key="profiles")
    with pytest.raises(IntentError) as ei:
        load_intent(doc, log_forwarding_catalog=lf)
    assert "spec.log_forwarding" in [p.path for p in ei.value.problems]


def test_omitted_optional_fields_skip_catalog_checks():
    # profile/log_forwarding omitted -> no validation even with catalogs present
    load_intent(valid_doc(), profile_catalog=_profile_cat(),
                log_forwarding_catalog=_profile_cat())


# ── Happy path ────────────────────────────────────────────────────────────
def test_valid_intent_parses():
    ar = load_intent(valid_doc())
    assert isinstance(ar, AccessRequest)
    assert ar.metadata.id == "REQ-2026-0417"
    assert ar.spec.action == "allow"
    assert ar.spec.source == [Endpoint("cidr", "10.20.1.0/24")]
    assert Endpoint("fqdn", "payments.internal") in ar.spec.destination
    assert Service("tcp", "443") in ar.spec.service
    assert ar.spec.log is True


def test_expiry_is_REJECTED_not_ignored():
    """Removed 2026-08-05. Rejected rather than dropped, because this loader
    IGNORES unknown metadata keys — so deleting the field alone would turn every
    existing `expires:` into a silent no-op, the "compiles clean, does nothing"
    failure this codebase treats as a bug.

    It modelled a lifecycle the platform does not run: the date never reached the
    firewall and no job ever removed an expired rule. On a Day-1 kind it was
    parsed and dropped entirely, since evidence bundles are AccessRequest-only.
    """
    doc = valid_doc()
    doc["metadata"]["expires"] = "2026-10-19"
    with pytest.raises(IntentError, match="does not model rule expiry"):
        load_intent(doc)


def test_log_defaults_true_and_expires_optional():
    doc = valid_doc()
    del doc["spec"]["log"]
    ar = load_intent(doc)
    assert ar.spec.log is True


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
        "web-tier": {"environment": "prod", "zone": "local",
                     "addresses": ["10.20.1.0/24"]},
        "payments": {"environment": "prod", "zone": "app",
                     "addresses": ["10.20.9.10/32"], "fqdns": ["pay.internal"]},
        "staging-x": {"environment": "staging", "zone": "local",
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
    # NOT icmp — that became valid in v1.40.0. A protocol this platform genuinely
    # cannot express is the case worth pinning.
    doc["spec"]["service"] = [{"protocol": "sctp", "port": "132"}]
    assert "spec.service[0].protocol" in problems(doc)


# ── ICMP: matched by application, not by port ─────────────────────────────
def test_icmp_needs_no_port():
    doc = valid_doc()
    doc["spec"]["service"] = [{"protocol": "icmp"}]
    ar = load_intent(doc)
    assert ar.spec.service[0].protocol == "icmp"
    assert ar.spec.service[0].port is None
    assert ar.spec.service[0].is_application_matched


def test_a_port_alongside_icmp_is_REJECTED_not_ignored():
    """ICMP has no ports. Accepting one would let a requester write a number that
    reads like a restriction and enforces nothing — the silently-dropped-field
    trap `_reject_unknown` exists to close."""
    doc = valid_doc()
    doc["spec"]["service"] = [{"protocol": "icmp", "port": "8"}]
    with pytest.raises(IntentError) as ei:
        load_intent(doc)
    hit = [p for p in ei.value.problems if p.path == "spec.service[0].port"]
    assert hit, [p.path for p in ei.value.problems]
    assert "no ports" in hit[0].message


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


# ── ZoneRequest security fields ───────────────────────────────────────────
def _zone_sec_doc(**spec):
    base = {"environment": "prod", "zone": "dmz", "type": "layer3", "interfaces": []}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "ZoneRequest",
        "metadata": {"id": "Z1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def test_zone_security_fields_load():
    from fwgitops.intent import load_intent
    sp = load_intent(_zone_sec_doc(
        protection_profile="best-practice", log_forwarding="log-best",
        user_id=True, device_id=False, dos_profile="dp", dos_log_forwarding="dl",
        user_acl={"include": ["corp\\jane"], "exclude": ["corp\\bob"]},
    )).spec
    assert sp.protection_profile == "best-practice" and sp.log_forwarding == "log-best"
    assert sp.user_id is True and sp.device_id is False
    assert sp.dos_profile == "dp" and sp.dos_log_forwarding == "dl"
    assert sp.user_acl.include == ["corp\\jane"] and sp.user_acl.exclude == ["corp\\bob"]
    assert sp.device_acl is None


def test_zone_security_fields_are_all_optional():
    from fwgitops.intent import load_intent
    sp = load_intent(_zone_sec_doc()).spec
    assert sp.protection_profile is None and sp.user_id is None and sp.user_acl is None


@pytest.mark.parametrize("spec,frag", [
    ({"user_id": "yes"}, "must be true or false"),
    ({"protection_profile": ""}, "non-empty string"),
    ({"user_acl": ["nope"]}, "must be a mapping"),
    ({"user_acl": {"includ": []}}, "unknown field"),
    ({"user_acl": {"include": [""]}}, "non-empty strings"),
])
def test_zone_security_field_bad_shapes_are_rejected(spec, frag):
    from fwgitops.intent import IntentError, load_intent
    with pytest.raises(IntentError) as e:
        load_intent(_zone_sec_doc(**spec))
    assert any(frag in str(p) for p in e.value.problems)


def test_zone_reference_names_are_catalog_validated():
    """A typo'd profile must fail at PR time, not at the device commit —
    the ADR-0003 rule for rules, now applied to zones. The loader used to build
    its collector WITHOUT catalogs, so nothing was ever checked."""
    from fwgitops.catalog import NameCatalog
    from fwgitops.intent import IntentError, load_intent
    cat = NameCatalog(kind="zone-protection profile", names=frozenset({"best-practice"}))
    load_intent(_zone_sec_doc(protection_profile="best-practice"), zone_protection_catalog=cat)
    with pytest.raises(IntentError) as e:
        load_intent(_zone_sec_doc(protection_profile="typo"), zone_protection_catalog=cat)
    assert any("zone-protection profile 'typo'" in str(p) for p in e.value.problems)


# ── unknown metadata keys are rejected, not ignored ───────────────────────
def test_an_unknown_metadata_key_is_REJECTED():
    """Silently dropping unknown keys is how a field stops working with nobody
    noticing. A typo like `justifcation:` at least fails, as the required field
    then reads as missing — but `tickets:`, or a field retired in a later
    version, reads as ACCEPTED and does nothing.

    The `expires` retirement made that concrete: removing it from the schema
    alone would have turned every existing `expires:` into a no-op. This closes
    the class rather than that one instance.
    """
    doc = valid_doc()
    doc["metadata"]["tickets"] = "JIRA-12345"
    with pytest.raises(IntentError, match=r"unknown field\(s\) \['tickets'\]"):
        load_intent(doc)


def test_the_error_names_the_keys_that_ARE_allowed():
    """A rejection that does not say what was expected just moves the guesswork."""
    doc = valid_doc()
    doc["metadata"]["justifcation"] = "typo"
    with pytest.raises(IntentError) as e:
        load_intent(doc)
    for expected in ("id", "requester", "ticket", "justification", "requested"):
        assert expected in str(e.value)


def test_a_retired_key_keeps_its_OWN_message():
    """`expires` explains what replaced it. Folding it into a generic unknown-key
    list would throw that explanation away exactly when someone needs it."""
    doc = valid_doc()
    doc["metadata"]["expires"] = "2026-10-19"
    with pytest.raises(IntentError, match="does not model rule expiry"):
        load_intent(doc)


def test_the_allowed_key_set_matches_the_dataclass():
    """If a field is added to `Metadata` and not to `_METADATA_KEYS`, every intent
    using it is rejected as unknown — the new field would be unusable, and the
    error would blame the author rather than the schema."""
    import dataclasses

    from fwgitops.intent import _METADATA_KEYS, Metadata
    assert {f.name for f in dataclasses.fields(Metadata)} == set(_METADATA_KEYS)


def test_every_shipped_intent_still_loads():
    """The guard is only safe if the repo's own intents pass it. A validation
    change that rejects the shipped tree would fail CI on the next PR, whoever
    opened it and whatever it touched."""
    from pathlib import Path

    import yaml as _yaml

    from fwgitops.catalog import FolderHierarchy, InterfaceCatalog, RouterCatalog
    from fwgitops.resolve import EnvMap
    root = Path(__file__).resolve().parents[1]
    kw = dict(
        env_map=EnvMap.from_dict(_yaml.safe_load((root / "catalog" / "environments.yaml").read_text())),
        folder_hierarchy=FolderHierarchy.from_dict(
            _yaml.safe_load((root / "catalog" / "folders.yaml").read_text())),
        interface_catalog=InterfaceCatalog.from_dict(
            _yaml.safe_load((root / "catalog" / "interfaces.yaml").read_text())),
        router_catalog=RouterCatalog.from_dict(
            _yaml.safe_load((root / "catalog" / "routers.yaml").read_text())),
    )
    seen = 0
    for path in sorted((root / "intent").rglob("*.yaml")):
        load_intent(_yaml.safe_load(path.read_text()), **kw)
        seen += 1
    assert seen >= 10


# ── unknown spec keys are rejected, not ignored ───────────────────────────
def _spec_keys_actually_read(src_path):
    """Every `spec:` key each loader really reads, by walking the AST.

    DISCOVERS accessors rather than hard-coding them: any `helper(sp, "key", ...)`
    counts, and any other helper taking `sp` first is followed. The first version
    of this hard-coded the accessor list, missed `_opt_positive_int`, and produced
    an allow-list that would have REJECTED the shipped default route — which sets
    `metric: 10` and always reached the firewall correctly.
    """
    import ast
    from pathlib import Path as _Path

    tree = ast.parse(_Path(src_path).read_text())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def keys_of(fn, seen):
        if fn.name in seen:
            return set()
        seen = seen | {fn.name}
        out = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "get"
                    and isinstance(f.value, ast.Name) and f.value.id == "sp"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
            if not isinstance(f, ast.Name):
                continue
            if not (node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "sp"):
                continue
            if (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                out.add(node.args[1].value)
            elif f.id == "_load_target":
                out |= {"folder", "device", "environment"}
            elif f.id in funcs:
                out |= keys_of(funcs[f.id], seen)
        return out

    return {name: keys_of(fn, set())
            for name, fn in funcs.items() if name.endswith("_spec")}


@pytest.mark.parametrize("loader,constant", [
    ("_load_spec", "_ACCESS_SPEC_KEYS"),
    ("_load_zone_spec", "_ZONE_SPEC_KEYS"),
    ("_load_interface_spec", "_INTERFACE_SPEC_KEYS"),
    ("_load_route_spec", "_ROUTE_SPEC_KEYS"),
])
def test_each_allow_list_matches_what_its_loader_actually_reads(loader, constant):
    """Both directions matter, and each is a different bug.

    A key the loader READS but that is MISSING from the allow-list rejects a
    valid intent — the worst outcome, because it blocks legitimate work and the
    error blames the author. That is not hypothetical: hand-listing these sets
    omitted `metric`, and the shipped default route stopped loading.

    A key LISTED but never read is a dead allowance that lets exactly the typo
    this guard exists to catch straight through.
    """
    from pathlib import Path as _Path

    import fwgitops.intent as mod

    read = _spec_keys_actually_read(_Path(mod.__file__))[loader]
    declared = set(getattr(mod, constant))
    assert declared == read, (
        f"{constant} and {loader} disagree — "
        f"only in loader: {sorted(read - declared)} (would REJECT valid intents); "
        f"only in list: {sorted(declared - read)} (dead allowance)")


def test_an_unknown_spec_key_is_REJECTED():
    """The sharper half of the metadata guard. `metadata:` is paperwork; `spec:`
    is firewall behaviour, so a dropped key is a rule that does not do what it
    says — and looks fine doing it."""
    doc = valid_doc()
    doc["spec"]["logging"] = True          # the field is `log`
    with pytest.raises(IntentError, match=r"unknown field\(s\) \['logging'\]"):
        load_intent(doc)


def test_a_plausible_typo_in_spec_does_not_silently_weaken_the_rule():
    """`log: true` is the difference between a logged allow and an unlogged one.
    Before this, `logging: true` compiled clean and produced a rule with logging
    at its default — no plan diff, no warning, no failed apply, and a rule weaker
    than the one that was approved."""
    doc = valid_doc()
    doc["spec"]["logging"] = True
    del doc["spec"]["log"]
    with pytest.raises(IntentError, match="does not do what it says"):
        load_intent(doc)


def test_icmp_cannot_be_MIXED_with_port_services_in_one_request():
    """`service` is a RULE-LEVEL list, so an ICMP entry forces the whole rule to
    `application-default` — which would silently re-interpret the tcp/udp entries
    beside it as their App-ID defaults rather than the ports requested."""
    doc = valid_doc()
    doc["spec"]["service"] = [{"protocol": "tcp", "port": "443"}, {"protocol": "icmp"}]
    with pytest.raises(IntentError) as ei:
        load_intent(doc)
    hit = [p for p in ei.value.problems if p.path == "spec.service"]
    assert hit, [p.path for p in ei.value.problems]
    assert "cannot mix" in hit[0].message and "separate requests" in hit[0].message


def test_every_example_in_the_requester_guide_actually_LOADS():
    """`docs/requesting-rules.md` tells a requester to copy an example whole. If
    one no longer validates, the first thing a new user does is get an error —
    and the guide has drifted before: it documented `expires` for three weeks
    after the field was REMOVED from the schema in v1.23.0, and described a
    `position` default that v2.0.0 changed.

    Copy-pasteable is a claim. This is the check."""
    import re
    from pathlib import Path

    import yaml as _yaml

    from fwgitops.resolve import EnvMap
    root = Path(__file__).resolve().parents[1]
    env = EnvMap.from_dict(_yaml.safe_load((root / "catalog" / "environments.yaml").read_text()))
    from fwgitops.catalog import (
        FolderHierarchy, InterfaceCatalog, RouterCatalog, ServiceCatalog,
    )

    def _cat(cls, name):
        f = root / "catalog" / name
        return cls.from_dict(_yaml.safe_load(f.read_text())) if f.is_file() else None

    kw = dict(
        env_map=env,
        folder_hierarchy=_cat(FolderHierarchy, "folders.yaml"),
        interface_catalog=_cat(InterfaceCatalog, "interfaces.yaml"),
        router_catalog=_cat(RouterCatalog, "routers.yaml"),
        service_catalog=_cat(ServiceCatalog, "services.yaml"),
    )
    text = (root / "docs" / "requesting-rules.md").read_text()
    # EVERY kind, not just AccessRequest. The guide gained Zone/Interface/Route
    # examples in v2.0.0, and an example a requester is told to copy is a claim
    # that it works.
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S) if "kind: " in b]
    assert len(blocks) >= 4, f"expected an example per kind, found {len(blocks)}"
    kinds = set()
    for b in blocks:
        doc = _yaml.safe_load(b)
        load_intent(doc, **kw)          # raises IntentError with the detail
        kinds.add(doc["kind"])
    assert kinds == {"AccessRequest", "ZoneRequest", "InterfaceRequest", "RouteRequest"}, (
        f"the guide must show every shipped kind; it shows {sorted(kinds)}")


def test_the_requester_guide_does_not_document_removed_fields():
    """`expires` was removed in v1.23.0 and is now REJECTED, not ignored. The
    guide listed it in the metadata table until v2.0.0 — telling requesters to
    write a field that fails their PR."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "requesting-rules.md").read_text()
    table = text[text.index("### `metadata`"):text.index("### `spec`")]
    assert "| `expires` |" not in table, "removed fields must not be in the field reference"


def test_every_example_in_the_folder_guide_LOADS_and_matches_the_real_intents():
    """`docs/building-a-folder.md` is a reconstruction of how prod-edge was
    actually built, and it says so. That is only honest if the YAML it shows
    still validates AND still matches the files it cites — otherwise it becomes
    the same kind of confident fiction as the `expires` field, which the
    requester guide documented for three weeks after the schema began rejecting
    it."""
    import re
    from pathlib import Path

    import yaml as _yaml

    from fwgitops.catalog import (
        FolderHierarchy, InterfaceCatalog, RouterCatalog, ServiceCatalog,
    )
    from fwgitops.resolve import EnvMap
    root = Path(__file__).resolve().parents[1]

    def _cat(cls, name):
        f = root / "catalog" / name
        return cls.from_dict(_yaml.safe_load(f.read_text())) if f.is_file() else None

    kw = dict(
        env_map=EnvMap.from_dict(
            _yaml.safe_load((root / "catalog" / "environments.yaml").read_text())),
        folder_hierarchy=_cat(FolderHierarchy, "folders.yaml"),
        interface_catalog=_cat(InterfaceCatalog, "interfaces.yaml"),
        router_catalog=_cat(RouterCatalog, "routers.yaml"),
        service_catalog=_cat(ServiceCatalog, "services.yaml"),
    )
    text = (root / "docs" / "building-a-folder.md").read_text()
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S) if "kind: " in b]
    assert len(blocks) >= 3, f"expected the three Day-1 kinds, found {len(blocks)}"

    seen = set()
    for b in blocks:
        doc = _yaml.safe_load(b)
        load_intent(doc, **kw)
        seen.add(doc["kind"])

        # And it must still describe the REAL file it cites. A guide that drifts
        # from the intent it claims to reconstruct is worse than one that
        # invented an example, because it reads as evidence.
        rid = doc["metadata"]["id"]
        real = list((root / "intent").rglob(f"{rid}.yaml"))
        assert real, f"{rid} is cited but no longer exists in intent/"
        actual = _yaml.safe_load(real[0].read_text())
        assert doc["spec"] == actual["spec"], (
            f"{rid}: the guide's spec no longer matches {real[0]}")

    assert seen == {"InterfaceRequest", "ZoneRequest", "RouteRequest"}, (
        f"the folder guide must walk the whole Day-1 chain; it shows {sorted(seen)}")
