"""Classifying what a PR REMOVES.

A deletion was invisible to the whole risk pipeline: `classify` reads the intent
TREE, and a deleted intent is simply absent, so nothing classified it and the
gate never saw it. Removing a rule that permits traffic and removing a route that
carries it were equally unassessed — not because anyone judged them low risk, but
because nothing judged them at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.cli import run_classify  # noqa: E402
from fwgitops.removal import Removal, classify_removal, find_removals  # noqa: E402

ENV = "prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n"


def _rule(rid, action="allow"):
    return (
        "apiVersion: fw-intent/v1\n"
        "kind: AccessRequest\n"
        f"metadata: {{id: {rid}, requester: m@corp, ticket: J-1, justification: x,"
        " requested: 2026-08-05}\n"
        "spec:\n"
        "  environment: prod\n"
        f"  action: {action}\n"
        "  source: [{cidr: 10.20.1.0/24}]\n"
        "  destination: [{cidr: 10.20.9.9/32}]\n"
        "  service: [{protocol: tcp, port: \"443\"}]\n"
    )


def _tree(root: Path, files: dict):
    (root / "prod").mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / "prod" / name).write_text(body)
    return root


def _classify(tmp_path, base_files, cur_files, **kw):
    base = _tree(tmp_path / "base", base_files)
    cur = _tree(tmp_path / "cur", cur_files)
    env = tmp_path / "env.yaml"
    env.write_text(ENV)
    return run_classify(cur, env, baseline_root=base, **kw)


# ── the asymmetry that makes a removal its own question ───────────────────
def test_removing_an_allow_is_LOW_and_removing_a_deny_is_HIGH(tmp_path, capsys):
    """Not mirror images. Removing an `allow` withdraws access — it can break
    what depended on it, but it opens nothing. Removing a `deny` does the
    opposite: traffic it blocked may now match a permissive rule below it."""
    _classify(tmp_path, {"A.yaml": _rule("ALLOW-1"), "D.yaml": _rule("DENY-1", "deny")},
              {"A.yaml": _rule("ALLOW-1")})
    out = capsys.readouterr().out
    assert "REMOVED DENY-1" in out
    assert "HIGH" in [ln.split()[2] for ln in out.splitlines() if "REMOVED DENY-1" in ln][0]

    _classify(tmp_path / "b", {"A.yaml": _rule("ALLOW-1")}, {})
    out2 = capsys.readouterr().out
    row = [ln for ln in out2.splitlines() if "REMOVED ALLOW-1" in ln][0]
    assert "LOW" in row and "allow_rule_removed" in row


def test_a_removal_participates_in_the_GATE(tmp_path, capsys):
    """The point of classifying it. Before this, a deletion could not exceed any
    tier because it was never tiered — so a route removal auto-applied."""
    rc = _classify(tmp_path, {"D.yaml": _rule("DENY-1", "deny")}, {}, gate="LOW")
    assert rc == 3
    assert "exceed max-auto-tier LOW" in capsys.readouterr().err


def test_a_removal_below_the_gate_still_passes(tmp_path, capsys):
    """A gate that blocks every deletion would just be turned off."""
    assert _classify(tmp_path, {"A.yaml": _rule("ALLOW-1")}, {}, gate="LOW") == 0


def test_deleting_EVERY_intent_is_still_classified(tmp_path, capsys):
    """The sharpest case, and the one an early return hid: an empty current tree
    used to report "no intent files found" and exit 0 — waving through the
    largest possible removal."""
    rc = _classify(tmp_path, {"D.yaml": _rule("DENY-1", "deny")}, {}, gate="LOW")
    assert rc == 3, "an empty intent tree must not bypass the gate"
    assert "REMOVED DENY-1" in capsys.readouterr().out


def test_no_baseline_means_no_removal_reporting(tmp_path, capsys):
    """Opt-in. Without a baseline there is nothing to diff against, and inventing
    removals from live state would report every hand-made object as a deletion."""
    cur = _tree(tmp_path / "cur", {"A.yaml": _rule("ALLOW-1")})
    env = tmp_path / "env.yaml"
    env.write_text(ENV)
    assert run_classify(cur, env) == 0
    assert "REMOVED" not in capsys.readouterr().out


def test_an_unreadable_baseline_FAILS_rather_than_reporting_zero(tmp_path, capsys):
    """Fail closed. Reporting "no removals" because the comparison broke is the
    exact blindness this feature removes."""
    base = _tree(tmp_path / "base", {"A.yaml": _rule("ALLOW-1")})
    (base / "prod" / "broken.yaml").write_text("kind: AccessRequest\nspec: {oops:\n")
    cur = _tree(tmp_path / "cur", {})
    env = tmp_path / "env.yaml"
    env.write_text(ENV)
    rc = run_classify(cur, env, baseline_root=base, gate="LOW")
    assert rc == 2
    assert "cannot be fully parsed" in capsys.readouterr().err


def test_a_missing_baseline_directory_is_an_error(tmp_path, capsys):
    cur = _tree(tmp_path / "cur", {"A.yaml": _rule("ALLOW-1")})
    env = tmp_path / "env.yaml"
    env.write_text(ENV)
    assert run_classify(cur, env, baseline_root=tmp_path / "nope") == 1
    assert "baseline intent tree not found" in capsys.readouterr().err


# ── per-kind tiers, unit level ────────────────────────────────────────────
@pytest.mark.parametrize("kind,expected,check", [
    ("RouteRequest", "HIGH", "route_removed"),
    ("ZoneRequest", "HIGH", "zone_removed"),
    ("InterfaceRequest", "HIGH", "interface_config_removed"),
])
def test_day1_removals_are_HIGH(kind, expected, check):
    """Each has an outage-shaped failure: a route stops forwarding, a zone leaves
    interfaces unzoned (PAN-OS drops that traffic), and an interface override
    reverts to an inherited object carrying no addressing — which is exactly what
    the re-onboard did on 2026-08-05."""
    v = classify_removal(Removal(kind=kind, req_id="X-1", request=object()))
    assert v.tier == expected
    assert v.checks_fired[0]["check"] == check


def test_an_unknown_kind_removal_is_CRITICAL():
    """Fail closed. A kind added later without a removal rule must be looked at,
    not waved through — a default that permits is how a class of change goes
    unassessed, which is the bug this module exists to fix."""
    v = classify_removal(Removal(kind="NatRequest", req_id="N-1", request=object()))
    assert v.tier == "CRITICAL"


def test_removals_key_on_kind_AND_id():
    """Keying on id alone would make a kind change look like neither a removal
    nor an addition."""
    base = {("ZoneRequest", "X"): Removal("ZoneRequest", "X", object())}
    assert find_removals(base, [("AccessRequest", "X")])
    assert not find_removals(base, [("ZoneRequest", "X")])


# ── a modified intent must carry its own change ticket ────────────────────
def _rule_t(rid, ticket, cidr="10.20.1.0/24", action="allow"):
    return (
        "apiVersion: fw-intent/v1\n"
        "kind: AccessRequest\n"
        f"metadata: {{id: {rid}, requester: m@corp, ticket: {ticket},"
        " justification: x, requested: 2026-08-06}\n"
        "spec:\n"
        "  environment: prod\n"
        f"  action: {action}\n"
        f"  source: [{{cidr: {cidr}}}]\n"
        "  destination: [{cidr: 10.20.9.9/32}]\n"
        "  service: [{protocol: tcp, port: \"443\"}]\n"
    )


def test_widening_a_rule_without_a_new_ticket_is_REJECTED(tmp_path, capsys):
    """`metadata` describes a REQUEST — a one-time event — while the file is a
    RULE, a long-lived object. Editing the object does not update the event
    record, so the evidence bundle for today's change names whoever asked for the
    ORIGINAL one.

    Measured 2026-08-08 on REQ-2026-0727: widening /24 -> /16 produced a bundle
    reading `ticket: JIRA-20727, requested: 2026-07-26` with a justification for
    the narrower rule. Only `intent_sha256` moved. The bundle claims NIST CM-3
    and named the wrong request — a false statement in a compliance artifact, so
    it fails the change rather than annotating it.
    """
    rc = _classify(tmp_path,
                   {"A.yaml": _rule_t("R-1", "JIRA-1")},
                   {"A.yaml": _rule_t("R-1", "JIRA-1", cidr="10.20.0.0/16")})
    assert rc == 2
    err = capsys.readouterr().err
    assert "reuse the previous change ticket" in err
    assert "JIRA-1" in err


def test_the_same_change_WITH_a_new_ticket_is_accepted(tmp_path, capsys):
    rc = _classify(tmp_path,
                   {"A.yaml": _rule_t("R-1", "JIRA-1")},
                   {"A.yaml": _rule_t("R-1", "JIRA-2", cidr="10.20.0.0/16")})
    assert rc == 0


def test_a_metadata_only_edit_does_NOT_demand_a_new_ticket(tmp_path, capsys):
    """Only a SPEC change alters the firewall. Demanding a ticket for a
    justification reword or a comment would make the rule fire on edits that
    change nothing — and a check that fires on nothing is one people route
    around."""
    before = _rule_t("R-1", "JIRA-1")
    after = before.replace("justification: x", "justification: a clearer reason")
    assert _classify(tmp_path, {"A.yaml": before}, {"A.yaml": after}) == 0


def test_comparison_is_SEMANTIC_not_textual(tmp_path, capsys):
    """Reformatting is not a change to the firewall. Comparing raw YAML would
    flag key reordering and whitespace, which is how a guard becomes noise."""
    before = _rule_t("R-1", "JIRA-1")
    after = before.replace(
        '  source: [{cidr: 10.20.1.0/24}]\n',
        '  source:\n    - cidr: 10.20.1.0/24\n')
    assert _classify(tmp_path, {"A.yaml": before}, {"A.yaml": after}) == 0


def test_an_unchanged_intent_alongside_a_changed_one_is_not_flagged(tmp_path, capsys):
    """Only the modified intent needs a new ticket — untouched neighbours must
    not be dragged in."""
    rc = _classify(
        tmp_path,
        {"A.yaml": _rule_t("R-1", "JIRA-1"), "B.yaml": _rule_t("R-2", "JIRA-9")},
        {"A.yaml": _rule_t("R-1", "JIRA-2", cidr="10.20.0.0/16"),
         "B.yaml": _rule_t("R-2", "JIRA-9")})
    assert rc == 0
