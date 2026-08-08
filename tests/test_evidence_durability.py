"""The audit record must survive, and must live in the source of truth.

`evidence.py` has always declared the design property:

    evidence/<folder>/<REQ-id>.json   (committed; Git = SSoT)

Until v1.34.0 the apply workflow only UPLOADED bundles as a run artifact, which
expires on GitHub's default retention. An audit trail with a TTL is not an audit
trail, and a stated design property that the pipeline does not keep is the same
class of defect as `expires` claiming an enforcement nothing performed.

These tests read the workflow, because that is where the property is kept or
lost.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
APPLY = REPO_ROOT / ".github" / "workflows" / "apply.yml"


def _workflow():
    return yaml.safe_load(APPLY.read_text())


def _steps():
    wf = _workflow()
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def test_evidence_bundles_are_COMMITTED_not_only_uploaded():
    """An uploaded artifact expires; a commit does not."""
    names = [s.get("name") or "" for s in _steps()]
    assert any("Commit evidence" in n for n in names), (
        "apply.yml must commit the bundles — an artifact-only audit trail has a "
        "retention TTL, which `evidence.py` explicitly does not claim")


def test_the_commit_step_can_actually_write():
    """`contents: read` would make the commit step fail at push time — after the
    firewall had already been changed, which is the worst moment to discover it."""
    assert _workflow()["permissions"]["contents"] == "write"


def test_committing_evidence_cannot_retrigger_the_workflow():
    """The trigger's `paths:` filter is what makes this safe. If `evidence/**`
    were ever added there, every apply would commit, retrigger, and apply again —
    an infinite loop that also re-pushes to the firewall."""
    paths = _workflow()[True]["push"]["paths"]
    assert not any(p.startswith("evidence") for p in paths), (
        f"evidence/ must not appear in the push paths filter: {paths}")
    assert paths, "a paths filter must exist — without one, every push retriggers"


def test_the_push_race_is_handled_but_a_conflict_is_not_swallowed():
    """Applies queue (concurrency group), so two runs can race to push. A rebase
    CONFLICT means two runs disagree about the same bundle — worth failing for,
    not auto-resolving, because auto-resolving silently drops one change's audit
    record."""
    step = next(s for s in _steps() if (s.get("name") or "").startswith("Commit evidence"))
    run = step["run"]
    assert "--rebase" in run, "a concurrent apply will have pushed first"
    assert "set -euo pipefail" in run, "a failed push must fail the step"
    assert "::error::" in run, "exhausting retries must surface as an error"


def test_the_bundle_path_is_one_file_per_rule():
    """This is what makes `git log evidence/<folder>/<REQ-id>.json` a rule's
    change history: each change overwrites the file, so each commit is one
    change, carrying the ticket that authorised it."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from fwgitops.evidence import bundle_path

    p = bundle_path("evidence",
                    {"req_id": "REQ-2026-0727", "compiled": {"folder": "prod-edge"}})
    assert p == Path("evidence/prod-edge/REQ-2026-0727.json")
