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


# ── an empty push is not free ─────────────────────────────────────────────
def test_the_push_is_skipped_when_nothing_was_staged():
    """MEASURED 2026-08-09: three consecutive pushes with NOTHING staged each
    minted a config version — v75 at 08:51, v76 and v77 at 09:29–09:30 — while
    terraform reported `0 added, 0 changed, 0 destroyed` every time.

    An empty push is therefore not free: it appends to the tenant's version
    history, which is the record an assessor is being asked to trust, and it
    makes "did my push commit anything?" unanswerable from the job, since the
    empty job and the real one are byte-identical apart from the id."""
    run = _apply_loop()
    assert 'if [ "${staged:-1}" -gt 0 ] || [ "${moved:-0}" -gt 0 ]; then' in run
    assert "skipping push" in run


def test_a_MOVE_counts_as_staged_even_when_terraform_saw_no_changes():
    """enrich applies ORDERING via the SCM API, which `terraform plan` cannot
    see. Deciding on plan alone would skip the push that commits a reorder."""
    run = _apply_loop()
    assert "select(.moved)" in run, "enrich's move count must feed the decision"


def test_an_UNREADABLE_enrich_output_pushes_rather_than_skipping():
    """Fail direction matters more than the check. Skipping on an unparseable
    enrich output would leave a move staged and uncommitted — the
    applied-but-unpushed state `devicesync.py` documents as invisible to every
    check in this repo. An unnecessary push costs a version entry; a missed one
    costs a silent divergence between SCM and the firewall."""
    run = _apply_loop()
    i = run.index('could not read enrich output')
    assert "moved=1" in run[i:i + 400], "unknown must mean PUSH, not skip"


def test_the_push_decision_reads_what_the_apply_DID_not_a_prediction():
    """REGRESSION, run 31308877939. v1.39.3 decided from
    `terraform plan -detailed-exitcode`. The plan file said `Plan: 3 to add`, the
    apply created three resources, and the push was SKIPPED anyway — leaving a
    new rule staged in SCM and never committed, which is exactly the
    applied-but-unpushed state `devicesync.py` calls invisible.

    The construct returns 2 correctly in isolation and the mechanism was never
    reproduced, so the dependency was REMOVED rather than patched: the decision
    now reads terraform's own report of the run that just happened."""
    run = _apply_loop()
    assert "plan_rc" not in run, (
        "the decision must not depend on a pre-apply prediction that was "
        "observed to be wrong and never explained")
    assert 'grep -q "Resources: 0 added, 0 changed, 0 destroyed"' in run


def test_an_unreadable_APPLY_summary_pushes_rather_than_skipping():
    """Same fail direction as the enrich output: unknown means push. A format
    change in terraform's summary must not silently turn into "nothing to do"."""
    run = _apply_loop()
    i = run.index("could not read the apply summary")
    assert "staged=1" in run[max(0, i - 500):i], "the default before the check must be PUSH"


# ── tag lifecycle (ADR-0009) ──────────────────────────────────────────────
def test_tags_are_ensured_BEFORE_apply_and_swept_AFTER_push():
    """The ordering IS the fix. Terraform ran a tag DESTROY before the rule
    UPDATE that released it and 409'd (spike/tag-destroy-ordering), so creation
    and removal are separated in time — and the sweep must never share an
    operation with the rule change that released the tag."""
    run = _apply_loop()
    i_ensure = run.index("fwgitops tags ensure")
    i_apply = run.index("terraform -chdir=\"$dir\" apply")
    i_push = run.index("fwgitops push --scope-dir")
    i_sweep = run.index("fwgitops tags sweep")
    assert i_ensure < i_apply, "tags must exist before the rules that reference them"
    assert i_push < i_sweep, "the sweep runs after the push, never before"


def test_the_sweep_cannot_fail_the_apply():
    """By the time it runs the firewall is already updated. Failing the job would
    turn leftover garbage into a red apply for a change that succeeded."""
    run = _apply_loop()
    i = run.index("fwgitops tags sweep")
    tail = run[i:i + 300]
    assert "||" in tail and "::warning::" in tail


# ── the drift schedule is gated on the firewall being up ──────────────────
def test_the_drift_SCHEDULE_is_gated_but_a_dispatch_is_not():
    """The pilot is suspended in AWS between test sessions. With it stopped every
    nightly run fails on device-sync and the SCM reads — and a red run each
    morning for a known-absent firewall is how a real alert gets ignored. This
    job's FAILURE IS THE ALERT, so its signal is worth more than its cadence.

    A manual dispatch must still run unconditionally: needing to flip a variable
    first would put friction in front of the check exactly when someone wants
    it."""
    import yaml
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "drift-detect.yml").read_text())
    cond = wf["jobs"]["drift"]["if"]
    assert "workflow_dispatch" in cond, "a deliberate dispatch must always run"
    assert "FIREWALL_ONLINE" in cond, "the schedule must be gated on the firewall being up"
    assert "schedule" in wf[True], "the cron itself stays — only the job is gated"


# ── tier routing: the approval policy is the pipeline, not a claim ────────
def test_the_environment_is_chosen_by_RISK_TIER():
    """A single shared environment made the approval policy uniform, and putting
    a required reviewer on it (2026-08-10, for CM-5) silently stopped LOW changes
    auto-applying — while README, DESIGN.md and the v2.0.0 release notes all
    still said they did. GitHub environment protection is job-level and cannot be
    conditional on a tier, so the tier has to pick the ENVIRONMENT."""
    wf = _workflow()
    assert "classify" in wf["jobs"], "a job must compute the tier before apply runs"
    assert wf["jobs"]["apply"]["needs"] == "classify"
    env = wf["jobs"]["apply"]["environment"]
    assert "needs.classify.outputs.tier" in env, "the environment must depend on the tier"
    assert "firewall-apply-auto" in env and "firewall-apply'" in env


def test_anything_that_is_not_LOW_routes_to_the_REVIEWED_environment():
    """Fail-safe direction. A classify job that failed, an empty output, or a
    tier the expression does not know must land on 'a human looks at it' — never
    on the unreviewed environment."""
    env = _workflow()["jobs"]["apply"]["environment"]
    assert "== 'LOW' && 'firewall-apply-auto' || 'firewall-apply'" in env, (
        "only an exact LOW may reach the unreviewed environment")


def test_the_tier_job_does_not_touch_the_firewall():
    """It runs before any approval, so it must be read-only: no SCM writes, no
    Terraform, no cloud credentials."""
    steps = _workflow()["jobs"]["classify"]["steps"]
    body = " ".join(str(s.get("run", "")) + str(s.get("uses", "")) for s in steps)
    for forbidden in ("terraform", "fwgitops push", "fwgitops apply", "aws-actions",
                      "fwgitops tags", "fwgitops enrich"):
        assert forbidden not in body, f"classify must not run {forbidden!r}"


def test_there_is_NO_human_entered_risk_ceiling():
    """`max_auto_tier` asked a human to RESTATE the tier the classifier had
    already computed — two sources of truth for one fact. Picking LOW on a HIGH
    change failed the run for no reason; habitually picking CRITICAL made the
    gate mean nothing. The tier is computed in code and routes to an approver;
    nobody types it."""
    wf = _workflow()
    assert wf[True]["workflow_dispatch"] in (None, {}), (
        "a manual re-run must take no risk knobs")
    steps = [s.get("name") or "" for s in wf["jobs"]["apply"]["steps"]]
    assert not any("Risk gate" in n for n in steps), (
        "routing plus approval is the control; a second ceiling is redundant")
    assert "max_auto_tier" not in yaml.dump(wf)


def test_the_PR_still_previews_the_tier():
    """Removing the apply-side gate must not remove the requester's feedback: the
    PR still reports what each change is worth, it just no longer blocks on a
    number a human typed."""
    pr = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "pr-validate.yml").read_text())
    body = " ".join(str(s.get("run", "")) for j in pr["jobs"].values() for s in j["steps"])
    assert "fwgitops classify" in body


def test_the_tier_is_computed_from_the_CHANGESET_not_the_whole_tree():
    """MEASURED 2026-08-10, and it made the routing inert. `--max-tier` without a
    baseline maximises over every intent that EXISTS, so `REQ-2026-0803` — a
    default route, permanently HIGH — meant every apply routed to the reviewed
    environment, including a changeset that was entirely LOW. LOW auto-apply was
    unreachable on any repo that had ever declared a default route.

    "How risky is this change?" is the question the approver is being asked."""
    job = _workflow()["jobs"]["classify"]
    assert job["steps"][0]["with"]["fetch-depth"] == 0, "a baseline needs full history"
    run = [s["run"] for s in job["steps"] if s.get("id") == "tier"][0]
    assert "--baseline" in run, "the tier must be computed against a baseline tree"
    assert "HEAD^" in run, "a dispatch has no github.event.before"
    assert "over-reports" in run, (
        "with no baseline it must fail toward review, and say so")


def test_prose_alone_cannot_trigger_an_apply():
    """OBSERVED 2026-08-10, in production. A one-paragraph edit to
    `intent/README.md` matched `intent/**` and ran a full apply against the live
    tenant. The apply itself was harmless — `0 added, 0 changed, 0 destroyed`,
    push correctly skipped — but it held the S3 state lock for 80 seconds and
    failed the `terraform plan` of an unrelated PR running at the time.

    A markdown file cannot change compiled Terraform; nothing in the compiler
    reads one. So every run a `.md` starts is either a no-op or a collision, and
    the collision is the one that costs: it makes a green PR go red for a reason
    that has nothing to do with the PR.

    ORDER MATTERS in a `paths:` filter — a negation subtracts from the includes
    ABOVE it, so `!**/*.md` first would exclude nothing."""
    paths = _workflow()[True]["push"]["paths"]   # `on:` parses as the bool True
    assert "!**/*.md" in paths, "a docs edit must not start an apply"
    assert paths.index("!**/*.md") > max(
        i for i, p in enumerate(paths) if not p.startswith("!")), (
        "the negation must come after the includes it subtracts from")
