"""Reordering a rulebase changes the policy without changing a rule.

The pilot's real numbers throughout. On 2026-08-16 the live rulebase was

    REQ-2026-0727, REQ-2026-0726, REQ-2026-0725, REQ-2026-0730,
    REQ-2026-0809, REQ-2026-0812

while the intents were deployed in the order 0725, 0726, 0727, 0730, 0809,
0812 — the first three reversed, with nothing in the system able to say whether
that was parallel creation or somebody dragging rules in the console.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from fwgitops.orderdrift import (
    OrderHistoryUnavailable,
    deployed_at,
    detect_order,
    expected_order,
    moves_to_restore,
)

DEPLOYED = ["REQ-2026-0725", "REQ-2026-0726", "REQ-2026-0727",
            "REQ-2026-0730", "REQ-2026-0809", "REQ-2026-0812"]
LIVE = ["REQ-2026-0727", "REQ-2026-0726", "REQ-2026-0725",
        "REQ-2026-0730", "REQ-2026-0809", "REQ-2026-0812"]


def test_the_pilots_real_rulebase_is_reported_as_out_of_order():
    r = detect_order(scope="prod-edge", expected=DEPLOYED, actual_rulebase=LIVE)
    assert not r.is_clean
    assert r.moved() == ["REQ-2026-0725", "REQ-2026-0727"], (
        "0726 sits at index 1 in both, so only the outer two actually moved")
    assert "RULE ORDER DRIFT" in r.summary()


def test_a_rulebase_in_deployment_order_is_clean():
    r = detect_order(scope="prod-edge", expected=DEPLOYED, actual_rulebase=DEPLOYED)
    assert r.is_clean
    assert "in deployment order" in r.summary()


def test_rules_this_platform_DID_NOT_CREATE_may_sit_anywhere():
    """A folder holds device-local and inherited rules, and where those sit is
    not ours to assert. Only our rules' MUTUAL order is policy — reporting on
    somebody else's rule would make the check unusable in a real folder, which
    is exactly where prod-edge keeps four."""
    interleaved = ["local-mgmt-allow", "REQ-2026-0725", "vendor-rule",
                   "REQ-2026-0726", "REQ-2026-0727", "cleanup-deny",
                   "REQ-2026-0730", "REQ-2026-0809", "REQ-2026-0812"]
    assert detect_order(scope="prod-edge", expected=DEPLOYED,
                        actual_rulebase=interleaved).is_clean


def test_a_rule_MISSING_from_the_rulebase_is_not_reported_as_a_reorder():
    """Absence is a different finding with a different remedy, and the tag
    engine already owns it. Reporting it here too would have an operator chasing
    an ordering problem that does not exist."""
    r = detect_order(scope="prod-edge", expected=DEPLOYED,
                     actual_rulebase=[n for n in LIVE if n != "REQ-2026-0730"])
    assert not r.is_clean
    assert "REQ-2026-0730" not in r.moved(), (
        "a rule that is gone has not MOVED; moved() must only name rules "
        "present in both")


# ── restoring ───────────────────────────────────────────────────────────────

def test_restoring_anchors_each_rule_to_the_PREVIOUS_one():
    """Not `bottom`.

    Moving each rule to the bottom in turn would also drag the whole managed
    block beneath any unmanaged rule sitting below it — changing this platform's
    relationship to config it does not own, in the name of fixing our own
    internal order. Anchoring leaves the first rule where it is.
    """
    moves = moves_to_restore(
        detect_order(scope="prod-edge", expected=DEPLOYED, actual_rulebase=LIVE))
    assert moves[0] == ("REQ-2026-0726", "REQ-2026-0725")
    assert moves[-1] == ("REQ-2026-0812", "REQ-2026-0809")
    assert all(after in DEPLOYED for _, after in moves)
    assert "REQ-2026-0725" not in [m for m, _ in moves], (
        "the first rule is the anchor and must not itself be moved")


def test_a_clean_rulebase_generates_NO_moves():
    """Or every apply would re-stack a rulebase that is already correct, writing
    to a live firewall for nothing."""
    assert moves_to_restore(
        detect_order(scope="p", expected=DEPLOYED, actual_rulebase=DEPLOYED)) == []


# ── where the expected order comes from ─────────────────────────────────────

def _repo(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    for i, name in enumerate(files):
        p = tmp_path / f"{name}.yaml"
        p.write_text("x\n")
        subprocess.run(["git", "add", p.name], cwd=tmp_path, check=True)
        # A COPY of the environment, not a replacement: git needs PATH and its
        # own config to run at all, and stripping it made this test fail for a
        # reason that had nothing to do with ordering.
        env = dict(os.environ, GIT_COMMITTER_DATE=f"2026-08-{10 + i}T00:00:00")
        subprocess.run(
            ["git", "commit", "-q", "-m", name,
             "--date", f"2026-08-{10 + i}T00:00:00"],
            cwd=tmp_path, check=True, env=env,
        )
    return tmp_path


def test_the_expected_order_is_WHEN_EACH_INTENT_LANDED(tmp_path):
    """Deployment order, read from Git. No manifest, nothing on disk to edit,
    and therefore nothing anybody can quietly amend to bless a reorder."""
    repo = _repo(tmp_path, ["REQ-b", "REQ-a", "REQ-c"])  # committed b, a, c
    intents = {n: repo / f"{n}.yaml" for n in ("REQ-a", "REQ-b", "REQ-c")}
    assert expected_order(intents, repo=repo) == ["REQ-b", "REQ-a", "REQ-c"], (
        "commit order, not alphabetical order")


def test_EDITING_an_intent_does_not_move_its_rule(tmp_path):
    """`--diff-filter=A`, last line. A rule edited today was not redeployed to
    the bottom; changing a source address does not restack a firewall."""
    # (This one passed even while `--follow` made every timestamp identical —
    # two rules in alphabetical order look correct by coincidence. The test
    # above is the one that caught it, because its commit order is deliberately
    # NOT alphabetical.)
    repo = _repo(tmp_path, ["REQ-a", "REQ-b"])
    (repo / "REQ-a.yaml").write_text("edited\n")
    subprocess.run(["git", "commit", "-qam", "edit a"], cwd=repo, check=True)
    intents = {n: repo / f"{n}.yaml" for n in ("REQ-a", "REQ-b")}
    assert expected_order(intents, repo=repo) == ["REQ-a", "REQ-b"]


def test_a_SHALLOW_CLONE_RAISES_rather_than_inventing_an_order(tmp_path):
    """The failure that would otherwise be silent and confident.

    `actions/checkout` defaults to `fetch-depth: 1`. With no history every
    intent looks deployed at the same instant, the expected order collapses to
    alphabetical, and the check reports drift on rules nobody touched — while
    looking exactly like a working control. It must refuse instead.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    orphan = tmp_path / "REQ-never-committed.yaml"
    orphan.write_text("x\n")
    with pytest.raises(OrderHistoryUnavailable, match="fetch-depth"):
        deployed_at(orphan, repo=tmp_path)


def test_rules_deployed_TOGETHER_break_ties_by_name(tmp_path):
    """"Append at bottom" is only deterministic one rule at a time. Three of the
    pilot's rules were created in a single apply before `-parallelism=1` existed
    and landed reversed. A tie must still produce a total, reproducible order or
    the check flaps between runs."""
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for n in ("REQ-c", "REQ-a", "REQ-b"):
        (repo / f"{n}.yaml").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "all three at once"], cwd=repo, check=True)

    intents = {n: repo / f"{n}.yaml" for n in ("REQ-c", "REQ-a", "REQ-b")}
    assert expected_order(intents, repo=repo) == ["REQ-a", "REQ-b", "REQ-c"]


# ── the revert half ─────────────────────────────────────────────────────────

class _FakeClient:
    """Records moves instead of issuing them."""

    def __init__(self, present):
        self._ids = {n: f"id-{n}" for n in present}
        self.moves = []

    def rule_ids_by_name(self, folder):
        return dict(self._ids)

    def move_rule(self, rule_id, *, destination, rulebase, target=None):
        self.moves.append((rule_id, destination, target))


def test_restoring_issues_ANCHORED_moves_in_deployment_order():
    from fwgitops.enrich import restore_deployment_order

    c = _FakeClient(DEPLOYED)
    moved = restore_deployment_order(c, "prod-edge", DEPLOYED)

    assert moved == DEPLOYED[1:], "every rule but the anchor is re-seated"
    assert all(d == "after" for _, d, _ in c.moves), (
        "anchored to the previous rule, never moved to `bottom` — bottom would "
        "drag the managed block beneath unmanaged rules below it")
    assert c.moves[0] == ("id-REQ-2026-0726", "after", "id-REQ-2026-0725")


def test_restoring_SKIPS_a_rule_that_is_not_in_the_folder():
    """Absence is a different finding, owned by the tag engine. Failing here
    would block the very apply that recreates the missing rule."""
    from fwgitops.enrich import restore_deployment_order

    c = _FakeClient([n for n in DEPLOYED if n != "REQ-2026-0727"])
    moved = restore_deployment_order(c, "prod-edge", DEPLOYED)

    assert "REQ-2026-0727" not in moved
    assert "REQ-2026-0730" not in moved, (
        "0730 anchors to the missing 0727, so it cannot be placed either — "
        "guessing a different anchor would invent an order nobody declared")


def test_the_evidence_bundles_CANNOT_order_anything():
    """Pinned because it is the obvious "improvement" to make here.

    Commit time records when an intent was MERGED; what one really wants is when
    the rule was APPLIED, and the evidence bundle looks like it knows. It does
    not — bundles are regenerated on every apply, so `generated_at` is the last
    one. Every rule in the repository shares a single value, which would collapse
    the expected order exactly as `--follow` did.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "evidence" / "prod-edge"
    # THE LIVE RULES ONLY. Bundles for requests that are gone keep whatever
    # timestamp their last apply wrote, so the directory as a whole does hold
    # several distinct values — checking all of them would "pass" for the wrong
    # reason and prove nothing about the rules being ordered.
    live = [root / f"{n}.json" for n in DEPLOYED]
    present = [p for p in live if p.is_file()]
    if len(present) < 2:
        return                      # nothing to prove on a fresh checkout
    stamps = {json.loads(p.read_text()).get("generated_at") for p in present}
    assert len(stamps) == 1, (
        "the bundles of currently-managed rules now carry distinct timestamps. "
        "If that is a real per-rule FIRST-applied time rather than an artefact "
        "of which rules the last apply happened to touch, orderdrift should use "
        "it in place of commit time")


# ── evidence ────────────────────────────────────────────────────────────────

def test_a_reorder_remediation_is_RECORDED_and_LINKED(tmp_path, monkeypatch):
    """A re-stack changes a live firewall and must leave what a deletion leaves.

    Until 2026-08-16 it left nothing: detection failed a run, remediation fixed
    it, and neither survived the CI log. Ordering was the only unauthorised
    change on this platform with no record and no link to the finding that
    justified acting on it — and it is the quietest class, since no rule is
    added, removed or edited.
    """
    import json

    from fwgitops import violations as _v
    from fwgitops.cli import _record_reorder

    vroot = tmp_path / "evidence" / "violations"
    _v.write(_v.reconcile(
        found=[{"cls": "reordered", "kind": "security-rule-order",
                "scope": "prod-edge", "name": "REQ-2026-0725", "tags": []}],
        existing={}, root=vroot, run_url="https://example/run/1",
        at="2026-08-16T01:00:00Z"))
    vid = json.loads(next(vroot.glob("*.json")).read_text())["id"]

    monkeypatch.chdir(tmp_path)
    out = []
    _record_reorder("prod-edge", DEPLOYED, ["REQ-2026-0725"],
                    out=type("W", (), {"write": lambda s, t: out.append(t)})())

    rec = json.loads(next((tmp_path / "evidence" / "manual-actions")
                          .glob("*.json")).read_text())
    assert rec["action"] == "reorder"
    assert rec["provenance"] == "workflow"
    assert rec["violation_id"] == vid, "the remediation must name what it fixed"
    assert "REQ-2026-0725" in rec["reason"]


def test_an_apply_that_MOVED_NOTHING_records_nothing(tmp_path, monkeypatch):
    """`restore_deployment_order` is idempotent and runs on EVERY apply.
    Recording unconditionally would file a remediation for each ordinary deploy
    and bury the ones that mean something."""
    monkeypatch.chdir(tmp_path)
    from fwgitops.cli import _record_reorder

    _record_reorder("prod-edge", DEPLOYED, [],
                    out=type("W", (), {"write": lambda s, t: None})())
    assert not (tmp_path / "evidence" / "manual-actions").exists()


def test_run_drift_ACTUALLY_WRITES_a_reordered_violation(tmp_path):
    """End to end through the real drift path, against this repository's own
    intents — because the wiring is the thing that was missing.

    Order drift printed a summary and failed the run while recording NOTHING,
    and a mutation removing the one line that feeds order findings into the
    violation recorder left the entire suite green. A test that asserts the line
    exists would be theatre; this runs the command and looks for the record.
    """
    import json
    import os
    from pathlib import Path

    from fwgitops.cli import run_drift

    repo = Path(__file__).resolve().parents[1]
    snap = tmp_path / "snap.json"
    # The pilot's rules, deliberately out of deployment order.
    wrong = ["REQ-2026-0812", "REQ-2026-0726", "REQ-2026-0727",
             "REQ-2026-0730", "REQ-2026-0809", "REQ-2026-0725"]
    snap.write_text(json.dumps([
        {"kind": "AccessRequest", "folder": "prod-edge", "scope": "prod-edge",
         "name": n, "tag": ["gitops:managed", f"gitops:req:{n}"]}
        for n in wrong
    ]))

    vroot = tmp_path / "violations"
    cwd = os.getcwd()
    os.chdir(repo)                       # git history is read from the repo
    try:
        rc = run_drift(repo / "intent", repo / "catalog" / "environments.yaml",
                       snap, service_catalog_path=repo / "catalog" / "services.yaml",
                       app_catalog_path=repo / "catalog" / "apps.yaml",
                       record_violations=vroot, run_url="https://example/run/1")
    finally:
        os.chdir(cwd)

    assert rc == 3, "a reordered rulebase must fail the run"
    recs = [json.loads(p.read_text()) for p in vroot.glob("*.json")]
    reordered = [r for r in recs if r["class"] == "reordered"]
    assert reordered, (
        "order drift produced no violation record — it fails the run and leaves "
        "nothing behind, which is the gap records exist to close")
    assert {r["name"] for r in reordered} >= {"REQ-2026-0725", "REQ-2026-0812"}
    assert all(r["id"].startswith("VIOL-") for r in reordered)
