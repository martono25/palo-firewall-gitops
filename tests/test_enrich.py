"""Tests for the ADR-0003 enrich step — writes the fields the scm provider drops.

Covers the orchestration (fail-closed, non-destructive opt-in, ordering) against a
fake RuleClient, plus the real ScmRuleClient's paths/bodies against a fake session.
"""

from __future__ import annotations

import json

import pytest

from fwgitops.clients import ScmRuleClient
from fwgitops.compiler import CompiledChange, SecurityRule
from fwgitops.enrich import EnrichError, enrich_folder
from fwgitops.scmapi import ScmCredentials, ScmSession


def _rule(name="R", *, application=("any",), profile_group=None, log_setting=None,
          relative_position="bottom", rulebase="pre", target_rule=None, folder="prod-edge"):
    return SecurityRule(
        name=name, folder=folder, from_zones=["local"], to_zones=["internet"],
        sources=["s"], destinations=["d"], services=["svc"], action="allow",
        log_end=True, tags=[], application=list(application), profile_group=profile_group,
        log_setting=log_setting, rulebase=rulebase, relative_position=relative_position,
        target_rule=target_rule,
    )


def _change(rule):
    return CompiledChange(address_objects=[], service_objects=[], rule=rule)


class FakeRuleClient:
    """Records what enrich would send to SCM."""

    def __init__(self, ids, current=None):
        self._ids = dict(ids)
        self._current = current or {}
        self.updates = []  # (id, body)
        self.moves = []    # (id, destination, rulebase, target)

    def rule_ids_by_name(self, folder):
        return dict(self._ids)

    def get_rule(self, rule_id):
        return dict(self._current.get(rule_id, {"id": rule_id, "name": "x"}))

    def update_rule(self, rule_id, body):
        self.updates.append((rule_id, body))

    def move_rule(self, rule_id, *, destination, rulebase, target=None):
        self.moves.append((rule_id, destination, rulebase, target))


# ── the four dropped fields get set ────────────────────────────────────────
def test_enrich_sets_all_fields():
    r = _rule("R", application=("ssl", "web-browsing"),
              profile_group="best-practice", log_setting="log-best")
    c = FakeRuleClient({"R": "id1"})
    res = enrich_folder(c, "prod-edge", [_change(r)])
    (rid, body), = c.updates
    assert rid == "id1"
    assert body["application"] == ["ssl", "web-browsing"]
    assert body["profile_setting"] == {"group": ["best-practice"]}
    assert body["log_setting"] == "log-best"
    assert res.records[0].name == "R"


def test_server_only_keys_stripped_from_put():
    c = FakeRuleClient({"R": "id1"}, current={"id1": {"id": "id1", "tfid": "x", "name": "R"}})
    enrich_folder(c, "prod-edge", [_change(_rule("R", profile_group="best-practice"))])
    (_, body), = c.updates
    assert "id" not in body and "tfid" not in body


# ── opt-in fields are NON-DESTRUCTIVE when the intent omits them ────────────
def test_omitted_optin_fields_preserve_current():
    r = _rule("R", log_setting=None, profile_group=None)  # neither declared
    c = FakeRuleClient({"R": "id1"}, current={"id1": {
        "id": "id1", "name": "R",
        "log_setting": "Cortex Data Lake",
        "profile_setting": {"group": ["existing"]},
    }})
    enrich_folder(c, "prod-edge", [_change(r)])
    (_, body), = c.updates
    assert body["log_setting"] == "Cortex Data Lake"          # preserved, not cleared
    assert body["profile_setting"] == {"group": ["existing"]}  # preserved
    assert body["application"] == ["any"]                      # declared default, always set


# ── ordering ───────────────────────────────────────────────────────────────
def test_top_issues_move():
    c = FakeRuleClient({"R": "id1"})
    res = enrich_folder(c, "prod-edge", [_change(_rule("R", relative_position="top"))])
    assert c.moves == [("id1", "top", "pre", None)]
    assert res.records[0].moved is True


def test_bottom_is_a_noop_move():
    c = FakeRuleClient({"R": "id1"})
    res = enrich_folder(c, "prod-edge", [_change(_rule("R", relative_position="bottom"))])
    assert c.moves == []
    assert res.records[0].moved is False


def test_before_resolves_target_and_moves():
    r = _rule("R", relative_position="before", target_rule="OTHER")
    c = FakeRuleClient({"R": "id1", "OTHER": "id2"})  # target exists in the folder
    res = enrich_folder(c, "prod-edge", [_change(r)])
    assert c.moves == [("id1", "before", "pre", "id2")]  # target resolved name->id
    assert res.records[0].moved is True
    assert res.records[0].position == "pre:before:OTHER"


def test_after_missing_target_fails_closed():
    r = _rule("R", relative_position="after", target_rule="GHOST")
    c = FakeRuleClient({"R": "id1"})  # GHOST not in folder
    with pytest.raises(EnrichError, match="target 'GHOST' not found"):
        enrich_folder(c, "prod-edge", [_change(r)])


# ── fail-closed on a missing skeleton ──────────────────────────────────────
def test_missing_rule_fails_closed():
    c = FakeRuleClient({})  # terraform didn't stage it
    with pytest.raises(EnrichError, match="not found"):
        enrich_folder(c, "prod-edge", [_change(_rule("R"))])
    assert c.updates == []  # nothing written


# ── idempotent ─────────────────────────────────────────────────────────────
def test_idempotent_put_body():
    r = _rule("R", application=("ssl",), profile_group="best-practice", log_setting="log-best")
    c = FakeRuleClient({"R": "id1"})
    enrich_folder(c, "prod-edge", [_change(r)])
    enrich_folder(c, "prod-edge", [_change(r)])
    assert c.updates[0][1] == c.updates[1][1]


def test_evidence_shape():
    r = _rule("R", application=("ssl",), profile_group="best-practice",
              log_setting="log-best", relative_position="top")
    res = enrich_folder(FakeRuleClient({"R": "id1"}), "prod-edge", [_change(r)])
    ev = res.to_evidence()
    assert ev["folder"] == "prod-edge"
    rec = ev["records"][0]
    assert rec["position"] == "pre:top" and rec["moved"] is True
    assert rec["profile_group"] == "best-practice"


# ── ScmRuleClient over a fake session (paths + bodies) ─────────────────────
GOOD = ScmCredentials(client_id="a@b.iam", client_secret="s", scope="tsg_id:1")


def recording_session(*payloads):
    responses = [(200, {"access_token": "t", "expires_in": 3600})]
    responses += [(200, p) for p in payloads]
    calls = []

    def transport(method, url, headers, body):
        try:  # the OAuth token POST sends a form body, not JSON
            parsed = json.loads(body) if body else None
        except (ValueError, TypeError):
            parsed = body
        calls.append((method, url, parsed))
        status, payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return status, json.dumps(payload).encode()

    s = ScmSession(GOOD, transport=transport)
    s.calls = calls  # type: ignore[attr-defined]
    return s


def test_client_rule_ids_by_name():
    s = recording_session({"data": [
        {"name": "REQ-1", "id": "u1"}, {"name": "REQ-2", "id": "u2"},
        {"name": "no-id"},  # skipped
    ]})
    assert ScmRuleClient(s).rule_ids_by_name("prod-edge") == {"REQ-1": "u1", "REQ-2": "u2"}
    method, url, _ = s.calls[-1]
    assert method == "GET" and "folder=prod-edge" in url and "position=pre" in url


def test_client_update_rule_puts_body():
    s = recording_session({})
    ScmRuleClient(s).update_rule("u1", {"application": ["ssl"]})
    method, url, body = s.calls[-1]
    assert method == "PUT" and url.endswith("/security-rules/u1")
    assert body == {"application": ["ssl"]}


def test_client_move_rule_body():
    s = recording_session({})
    ScmRuleClient(s).move_rule("u1", destination="top", rulebase="pre")
    method, url, body = s.calls[-1]
    assert method == "POST" and url.endswith("/security-rules/u1:move")
    assert body == {"destination": "top", "rulebase": "pre"}


def test_client_move_before_includes_target():
    s = recording_session({})
    ScmRuleClient(s).move_rule("u1", destination="before", rulebase="pre", target="u2")
    _, _, body = s.calls[-1]
    assert body["destination_rule"] == "u2"
