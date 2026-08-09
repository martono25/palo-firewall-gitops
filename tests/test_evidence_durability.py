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
    """This is what makes `git log evidence/<scope>/<REQ-id>.json` a request's
    change history: each change overwrites the file, so each commit is one
    change, carrying the ticket that authorised it.

    The PATH was only half of it. Until v1.36.1 every apply regenerated every
    bundle — `generated_at` always moves — so the workflow committed all of them
    and the log was a log of APPLIES, not of changes to that request. The other
    half is `write_bundle_if_changed`, asserted below and end to end in
    test_cli.py."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from fwgitops.evidence import bundle_path

    p = bundle_path("evidence", {"req_id": "REQ-2026-0727",
                                 "compiled": {"scope": {"kind": "folder",
                                                        "value": "prod-edge"}}})
    assert p == Path("evidence/prod-edge/REQ-2026-0727.json")


def test_an_unchanged_record_is_not_rewritten():
    """The commit step's `git diff --cached --quiet` is what turns this into "no
    commit". Without it, ten records were committed on every apply, each stamped
    with a run that had touched one of them."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from fwgitops.evidence import describes_same_change

    old = {"schema": "fw-evidence/v2", "kind": "AccessRequest", "status": "applied",
           "request": {"intent_sha256": "a" * 64},
           "compiled": {"object_sha256": "b" * 64},
           "generated_at": "2026-07-01T00:00:00Z",
           "apply": {"run_url": "https://gh/runs/1"}}
    new = dict(old, generated_at="2026-09-01T00:00:00Z",
               apply={"run_url": "https://gh/runs/999"})
    assert describes_same_change(new, old), (
        "a different run and time is not a different change — rewriting here is "
        "what backdated a run onto a request it never touched")


def test_the_commit_step_tolerates_having_nothing_to_commit():
    """With unchanged records preserved, "nothing to commit" is now the COMMON
    outcome of an apply that changed one request. If the step failed on it, the
    fix would surface as a red apply after the firewall had already changed."""
    step = next(s for s in _steps() if (s.get("name") or "").startswith("Commit evidence"))
    run = step["run"]
    assert "git diff --cached --quiet" in run and "exit 0" in run


def test_a_device_scoped_bundle_lands_under_its_own_directory():
    """A firewall is addressed `device=<serial>`, never `folder=<serial>`. Keying
    the path on scope keeps a device-scoped change out of a directory named for a
    serial — and mirrors the Terraform root layout, so `terraform/device-<s>/` and
    `evidence/device-<s>/` describe the same thing."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from fwgitops.evidence import bundle_path

    p = bundle_path("evidence", {"req_id": "REQ-2026-0801",
                                 "compiled": {"scope": {"kind": "device",
                                                        "value": "007955000894453"}}})
    assert p == Path("evidence/device-007955000894453/REQ-2026-0801.json")


# ── the approval evidence must actually be COLLECTED ──────────────────────
def _step(name_prefix: str):
    return next(s for s in _steps() if (s.get("name") or "").startswith(name_prefix))


def test_the_workflow_collects_who_approved():
    """`approvers` was hard-coded empty and no caller passed one, so every bundle
    ever written claimed CM-5 and named nobody. The Python side now declines the
    claim when it has no approver — which is honest, and useless if the workflow
    never collects any. This asserts the other half."""
    run = _step("Collect approval evidence")["run"]
    assert "--jq" in run and "reviews" in run, "PR review approvals must be collected"
    assert "approvals" in run, "the environment gate's approvers must be collected"
    assert "pull_request_review" in run and "deployment_gate" in run, (
        "the two routes must stay distinguishable — one person doing both is a "
        "finding, not a detail")
    assert "--approver" in _step("Generate evidence bundles")["run"]


def test_collecting_approvals_needs_the_permissions_to_read_them():
    """Without these the API 403s, no approver is collected, and the bundle
    quietly stops claiming CM-5 — a control lost to a missing scope."""
    perms = _workflow()["permissions"]
    assert perms.get("pull-requests") == "read"
    assert perms.get("actions") == "read"


def test_a_stray_api_line_cannot_become_an_APPROVER_NAME():
    """`sed` appends the route to whatever arrives, so an error string or an
    empty `[]` on stdout would be recorded as a person who approved a firewall
    change. A fabricated approver is worse than a missing one."""
    run = _step("Collect approval evidence")["run"]
    assert "logins()" in run and "grep -E" in run, (
        "collected lines must be filtered to well-formed GitHub logins")


def test_no_approver_is_surfaced_rather_than_silently_dropping_the_control():
    """An unapproved auto-apply of a LOW change is the designed path, so this is
    not a failure — but a bundle silently losing CM-5 is indistinguishable from a
    broken token, and that is the distinction the warning preserves."""
    run = _step("Collect approval evidence")["run"]
    assert "::warning::" in run and "CM-5" in run


# ── the apply loop must not be keyed on RULES ─────────────────────────────
def _apply_loop() -> str:
    return _step("terraform apply (stage) + SCM push")["run"]


def test_a_scope_with_no_RULES_is_still_applied():
    """MEASURED on the first successful apply (2026-08-09, run 31304463821): the
    loop guarded on `rules.auto.tfvars.json`, printed "no rules in
    device-007955000894453 — skip", and Terraform never ran against a root
    holding three InterfaceRequests. The job reported success.

    Nothing broke that day only because those interfaces had already been applied
    by hand. Any later change to a device-scoped interface would silently never
    reach the firewall — and interfaces are the FIRST link in ADR-0002's ordered
    chain, so the ordering this loop exists to honour was not being executed for
    that scope at all."""
    run = _apply_loop()
    assert '[ -f "$dir/rules.auto.tfvars.json" ] || { echo "no rules' not in run, (
        "the apply must not be gated on a rules file — same defect class as "
        "_compile_intents returning AccessRequest only")
    assert '*.auto.tfvars.json' in run, "apply any scope with compiled output"


def test_enrich_stays_rules_only_but_does_not_gate_the_apply():
    """Enrich IS rules-shaped — that is fine. Conflating "this step needs rules"
    with "this scope needs applying" is what skipped the whole root."""
    run = _apply_loop()
    i_apply = run.index("terraform -chdir=")
    i_enrich = run.index("fwgitops enrich")
    assert i_apply < i_enrich, "apply comes first"
    assert 'if [ -f "$dir/rules.auto.tfvars.json" ]; then' in run, (
        "the rules guard belongs on enrich, not on the loop")


def test_push_resolves_the_scope_rather_than_stripping_a_prefix():
    """`device-<serial>` is a Terraform root DIRECTORY; SCM rejects it as a
    folder. Stripping the prefix in YAML would put a second copy of the mapping
    outside `Scope.from_dirname` — the duplication that broke drift in v1.34.2."""
    run = _apply_loop()
    assert "fwgitops push --scope-dir" in run
    assert "${folder#device-}" not in run and "${folder#device_}" not in run


# ── removals must be recorded on the path they actually arrive by ─────────
def test_the_baseline_is_materialised_on_a_DISPATCH_too():
    """The condition was `if: github.event_name == 'push'`, which is inverted
    against how removals reach production: a route or zone removal classifies
    HIGH, and HIGH REQUIRES a manual dispatch to clear the risk gate. So the one
    case that most needs a tombstone was the one case that could not produce
    one."""
    step = _step("Materialise the baseline intent tree")
    assert "if" not in step or "github.event_name" not in str(step.get("if", "")), (
        "the baseline step must not be push-only")
    run = step["run"]
    assert "HEAD^" in run, (
        "`github.event.before` is empty on a dispatch, so the previous commit "
        "must come from git")
    assert "git log -1 --format=%B" in run, (
        "the `Removes:` trailer must still be readable on a dispatch")
