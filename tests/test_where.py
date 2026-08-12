"""`fwgitops where` — mapping an address back to the intent that authorised it.

The command exists because `grep` answers the incident-response question WRONG,
not slowly: the log says `10.20.9.10`, the intent says `10.20.9.0/24`, and grep
returns nothing. Returning nothing is the worst available answer, because it is
indistinguishable from "no rule permits this" — the conclusion someone will draw
at 3am with a firewall in front of them.
"""

from __future__ import annotations

import re

import ipaddress
from pathlib import Path

import pytest

from fwgitops.cli import run_where
from fwgitops.where import Hit, Query, find, mark_effective_routes, match_value, walk

ENV = "prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n"


def _rule(rid, src="10.20.1.0/24", dst="10.20.9.0/24", action="allow"):
    return (
        "apiVersion: fw-intent/v1\n"
        "kind: AccessRequest\n"
        f"metadata: {{id: {rid}, requester: m@corp, ticket: J-{rid}, justification: x,"
        " requested: 2026-08-05}\n"
        "spec:\n"
        "  environment: prod\n"
        f"  action: {action}\n"
        f"  source: [{{cidr: {src}}}]\n"
        f"  destination: [{{cidr: {dst}}}]\n"
        "  service: [{protocol: tcp, port: \"443\"}]\n"
    )


def _route(rid, dest, nexthop="10.100.2.1"):
    return (
        "apiVersion: fw-intent/v1\n"
        "kind: RouteRequest\n"
        f"metadata: {{id: {rid}, requester: m@corp, ticket: J-{rid}, justification: x,"
        " requested: 2026-08-05}\n"
        f"spec: {{environment: prod, destination: {dest}, nexthop: {nexthop}}}\n"
    )



def _named_for_id(name, body):
    """The file name a body's `metadata.id` requires.

    Fixtures used short names (`A.yaml`) while their bodies declared ids like
    `REQ-1`. The product rejects that — the id names the rule in SCM and the
    evidence file, the file name is what a human searches, and they drifted live
    on 2026-08-11. A fixture that ignores the rule under test stops modelling
    reality, so the name is derived and the dict key is only a label.

    PARSED, NOT PATTERN-MATCHED. The first version used a regex for `^  id:` and
    silently returned the original name for every fixture writing `metadata:
    {id: …}` in flow style — which was most of them, so 42 tests stayed red and
    looked like the product was wrong.
    """
    import yaml as _yaml
    try:
        doc = _yaml.safe_load(body) or {}
        rid = (doc.get("metadata") or {}).get("id")
    except Exception:  # noqa: BLE001 - a deliberately malformed fixture keeps its name
        return name
    return f"{rid}.yaml" if rid else name


def _named_write(directory, body):
    """Write under the name the body's id requires — see `_named_for_id`."""
    p = directory / _named_for_id("unused.yaml", body)
    p.write_text(body)
    return p


def _setup(tmp_path, files):
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / _named_for_id(name, body)).write_text(body)
    env = tmp_path / "env.yaml"
    env.write_text(ENV)
    return tmp_path / "intent", env


def _run(tmp_path, files, query, **kw):
    intent_root, env = _setup(tmp_path, files)
    return run_where(query, intent_root, env, **kw)


# ── the headline: containment, not text ───────────────────────────────────
def test_a_host_inside_a_declared_CIDR_is_found(tmp_path, capsys):
    """What grep cannot do. The intent never contains the string `10.20.1.55`."""
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1")})
    assert "10.20.1.55" not in (intent_root / "prod" / "REQ-1.yaml").read_text(), (
        "the fixture must NOT contain the query literally, or this proves nothing")
    assert run_where("10.20.1.55", intent_root, env) == 0
    o = capsys.readouterr().out
    assert "REQ-1" in o
    assert "10.20.1.0/24 contains 10.20.1.55" in o, "the reason must be stated"


def test_containment_works_in_BOTH_directions(tmp_path, capsys):
    """A responder may hold a host from a log (inside the intent's /24) or a
    subnet from a change request (containing it). Answering only one of those
    would cover half the questions asked and look complete doing it."""
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1", src="10.20.1.0/24")})
    assert run_where("10.20.0.0/16", intent_root, env) == 0
    assert "is inside the queried range" in capsys.readouterr().out


def test_an_address_no_rule_mentions_reports_NO_RULE_loudly(tmp_path, capsys):
    """The trap. A default route matches EVERY address, so a flat match count
    would say "1 match" for traffic nothing permits — the opposite of the truth.
    What permits and what carries are answered separately."""
    intent_root, env = _setup(tmp_path, {
        "A.yaml": _rule("REQ-1", src="10.20.1.0/24", dst="10.20.9.0/24"),
        "R.yaml": _route("REQ-2", "0.0.0.0/0")})
    run_where("203.0.113.5", intent_root, env)
    o = capsys.readouterr().out
    assert "RULES — what permits or denies it" in o
    assert "NONE" in o, "a silent rulebase must be stated, not implied by absence"
    assert "ROUTES — what carries it" in o and "REQ-2" in o


def test_nothing_at_all_is_an_ANSWER_not_an_error(tmp_path, capsys):
    """"No intent accounts for this" means the config came from somewhere else —
    which is a finding. It must not read like the command failed."""
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1")})
    rc = run_where("not-a-thing-here", intent_root, env)
    o = capsys.readouterr().out
    assert rc == 4
    assert "This is an ANSWER, not an error" in o
    assert "fwgitops drift" in o, "point at the tool that explains unowned config"


# ── precision ─────────────────────────────────────────────────────────────
def test_a_name_match_is_EXACT_not_substring():
    """A responder acting on the wrong zone is worse off than one who got no
    answer, so `dmz` must not hit `dmz-legacy`."""
    q = Query.parse("dmz")
    assert match_value(q, "name", "dmz")
    assert match_value(q, "name", "dmz-legacy") is None
    assert match_value(q, "name", "DMZ"), "case is not meaningful in SCM names"


def test_ipv4_and_ipv6_do_not_cross_match():
    q = Query.parse("10.20.1.55")
    assert match_value(q, "x", "2001:db8::/32") is None


def test_a_non_address_string_never_raises():
    """Most values in a compiled object are not addresses. A parse failure must
    be a non-match, not a crash mid-incident."""
    q = Query.parse("10.20.1.55")
    for junk in ("any", "", "ethernet1/1", "$eth-local", "tcp/443", "10.20.1.999"):
        assert match_value(q, "x", junk) is None


def test_walk_reports_the_index_that_matched():
    """"Which of the three destinations" is the difference between an answer and
    a shrug."""
    paths = dict(walk({"rule": {"destinations": ["a", "b", "c"]}}))
    assert paths["rule.destinations[1]"] == "b"


# ── routes: which one actually carries it ─────────────────────────────────
def test_the_LONGEST_PREFIX_route_is_flagged(tmp_path, capsys):
    """A default route matches everything, so listing matches without saying
    which wins is noise."""
    intent_root, env = _setup(tmp_path, {
        "R1.yaml": _route("REQ-DEFAULT", "0.0.0.0/0"),
        "R2.yaml": _route("REQ-SPECIFIC", "10.20.0.0/16")})
    run_where("10.20.1.55", intent_root, env)
    o = capsys.readouterr().out
    carries = [ln for ln in o.splitlines() if "CARRIES IT" in ln]
    assert len(carries) == 1, f"exactly one route carries it, got: {carries}"
    assert "REQ-SPECIFIC" in carries[0], "the more specific route wins"


def test_no_effective_route_is_invented_for_a_RANGE_query():
    """A /16 has no single effective route. Naming one would be a confident wrong
    answer — the failure this command exists to prevent."""
    hits = [Hit("RouteRequest", "R1", "f", "destination", "0.0.0.0/0", "why"),
            Hit("RouteRequest", "R2", "f", "destination", "10.0.0.0/8", "why")]
    marked = mark_effective_routes(hits, Query.parse("10.20.0.0/16"))
    assert not any(h.effective_route for h in marked)


def test_routes_in_different_scopes_do_not_compete():
    """Different scopes are different firewalls. A specific route on one does not
    stop the other's default from carrying the traffic there."""
    hits = [Hit("RouteRequest", "R1", "prod-edge", "destination", "0.0.0.0/0", "w"),
            Hit("RouteRequest", "R2", "device-007", "destination", "10.20.0.0/16", "w")]
    marked = mark_effective_routes(hits, Query.parse("10.20.1.55"))
    assert [h.req_id for h in marked if h.effective_route] == ["R1", "R2"]


# ── it searches the COMPILED state, not the YAML ──────────────────────────
def test_an_app_backed_rule_is_found_by_the_apps_ADDRESS(tmp_path, capsys):
    """The intent says `app: payments`; the CIDR lives in the catalog and appears
    nowhere in the intent file. Searching the YAML would miss every app-based
    rule — precisely the indirection the catalog exists to provide."""
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    _named_write(root, 
        "apiVersion: fw-intent/v1\n"
        "kind: AccessRequest\n"
        "metadata: {id: REQ-APP, requester: m@corp, ticket: J-1, justification: x,"
        " requested: 2026-08-05}\n"
        "spec:\n"
        "  environment: prod\n"
        "  action: allow\n"
        "  source: [{app: payments}]\n"
        "  destination: [{cidr: 10.99.9.9/32}]\n"
        "  service: [{protocol: tcp, port: \"443\"}]\n")
    env = tmp_path / "env.yaml"
    env.write_text("prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n")
    apps = tmp_path / "apps.yaml"
    apps.write_text("apps:\n  payments: {environment: prod, zone: local, "
                    "addresses: [10.77.3.0/24]}\n")
    assert "10.77.3" not in (root / "REQ-APP.yaml").read_text()

    rc = run_where("10.77.3.42", tmp_path / "intent", env, app_catalog_path=apps)
    o = capsys.readouterr().out
    assert rc == 0 and "REQ-APP" in o
    assert "10.77.3.0/24 contains 10.77.3.42" in o


# ── joining back to the audit record ──────────────────────────────────────
def test_a_missing_evidence_bundle_is_reported_as_a_FINDING(tmp_path, capsys):
    """A live object with no audit record is exactly what an assessor wants
    flagged. Hiding the path because the file is absent would hide that."""
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1")})
    run_where("10.20.1.55", intent_root, env, evidence_root=tmp_path / "nope")
    assert "MISSING — this change has no audit record" in capsys.readouterr().out


def test_json_output_is_machine_readable(tmp_path, capsys):
    import json
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1")})
    assert run_where("10.20.1.55", intent_root, env, as_json=True) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["req_id"] == "REQ-1"
    assert rows[0]["ticket"] and rows[0]["matched"]["why"]


def test_a_ticket_finds_what_it_changed(tmp_path, capsys):
    """The other direction a responder arrives from: a ticket, not an address."""
    intent_root, env = _setup(tmp_path, {"A.yaml": _rule("REQ-1")})
    assert run_where("J-REQ-1", intent_root, env) == 0
    assert "metadata.ticket is exactly" in capsys.readouterr().out
