"""Tests for the NIST-mapped evidence bundle."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fwgitops.compiler import compile_request
from fwgitops.evidence import (
    APPROVAL_CONTROL,
    DUAL_CONTROL,
    Approver,
    EVIDENCE_SCHEMA,
    CIContext,
    EvidenceError,
    RiskVerdict,
    build_bundle,
    dumps,
    sha256_bytes,
    write_bundle,
)
from fwgitops.intent import load_intent
from fwgitops.push import PushResult
from fwgitops.resolve import EnvMap

from test_intent import valid_doc

WHEN = datetime(2026, 7, 19, 10, 23, 0, tzinfo=timezone.utc)


def env_map():
    return EnvMap.from_dict({"prod": {"folder": "prod-edge", "from_zone": "trust", "to_zone": "app"}})


def pieces(doc=None):
    ar = load_intent(doc or valid_doc())
    return ar, compile_request(ar, env_map())


def bundle(**kw):
    ar, ch = pieces()
    base = dict(
        request=ar, compiled=ch, status="applied", generated_at=WHEN,
        intent_sha256=sha256_bytes(b"intent"), intent_path="intent/prod/x.yaml",
        tfvars_sha256=sha256_bytes(b"tfvars"), plan_sha256=sha256_bytes(b"plan"),
        ci=CIContext(pr_url="https://gh/pr/42", merge_commit="abc123",
                     run_url="https://gh/runs/9", gate="firewall-apply",
                     approvers=("alice@corp",)),
        push=PushResult(folder="prod-edge", status="success", job_id="job-1",
                        admins=("svc@example.iam.panserviceaccount.com",)),
    )
    base.update(kw)
    return build_bundle(**base)


# ── Structure + content ───────────────────────────────────────────────────
def test_bundle_is_self_contained():
    b = bundle()
    assert b["schema"] == EVIDENCE_SCHEMA
    assert b["req_id"] == "REQ-2026-0417"
    assert b["status"] == "applied"
    assert b["generated_at"] == "2026-07-19T10:23:00Z"
    # Every section an assessor needs, present without reading any other system.
    for section in ("request", "compiled", "risk", "approval", "apply", "push", "controls"):
        assert section in b


def test_request_section_carries_provenance():
    r = bundle()["request"]
    assert r["requester"] == "jane.doe@corp"
    assert r["ticket"] == "JIRA-12345"           # audit linkage
    assert r["justification"]
    assert "expires" not in r, "expiry was removed from the schema in v1.23.0"
    assert len(r["intent_sha256"]) == 64          # hash, not a copy


def test_compiled_section_records_what_was_built():
    c = bundle()["compiled"]
    assert c["scope"] == {"kind": "folder", "value": "prod-edge"}
    rule = c["object"]["rule"]
    assert rule["from_zones"] == ["trust"] and rule["to_zones"] == ["app"]
    assert rule["action"] == "allow"
    assert any(t.startswith("gitops:req:") for t in rule["tags"])
    assert c["compiler_version"]                  # reproducibility
    assert c["tfvars_file"] == "rules.auto.tfvars.json"
    assert len(c["tfvars_sha256"]) == 64


def test_bundle_names_its_kind():
    """v1 had no `kind`, because there was only ever one. A reader must not have
    to infer it from which fields happen to be present."""
    assert bundle()["kind"] == "AccessRequest"


def test_request_section_is_paperwork_only():
    """`action` and `environment` describe the FIREWALL, so they belong under
    `compiled` — derived from the spec, unable to disagree with it. In v1 they sat
    in `request` beside the ticket, which is the same conflation that let an
    edited rule keep the ticket authorising the previous version of itself."""
    r = bundle()["request"]
    assert set(r) == {"requester", "ticket", "justification", "requested",
                      "intent_file", "intent_sha256"}
    assert bundle()["compiled"]["object"]["rule"]["action"] == "allow"


def test_compiled_rule_records_adr0003_enrichment():
    r = bundle()["compiled"]["object"]["rule"]
    # the effective (enriched) rule an assessor reads — set on-device by enrich
    for k in ("application", "profile_group", "log_setting",
              "rulebase", "relative_position", "target_rule"):
        assert k in r
    assert r["application"] == ["any"]             # default when intent omits App-ID
    assert r["rulebase"] == "pre"
    # v1.0 completeness fields present
    for k in ("description", "log_start", "source_user", "category",
              "negate_source", "negate_destination"):
        assert k in r
    assert r["source_user"] == ["any"] and r["category"] == ["any"]


def test_approval_and_apply_sections():
    b = bundle()
    assert b["approval"]["approvers"] == [
        {"login": "alice@corp", "via": "unspecified"}]
    assert b["approval"]["pr"] == "https://gh/pr/42"
    assert b["approval"]["merge_commit"] == "abc123"
    assert b["apply"]["run_url"] == "https://gh/runs/9"


def test_push_section_from_push_result():
    assert bundle()["push"]["status"] == "success"
    assert bundle()["push"]["job_id"] == "job-1"


# ── Controls ──────────────────────────────────────────────────────────────
def test_base_controls_present():
    assert set(bundle()["controls"]) >= {"AC-4", "CM-3", "AU-2", "AU-12", "SC-7"}


def test_dual_control_added_for_critical_tier():
    b = bundle(risk=RiskVerdict(tier="CRITICAL", classifier_version="1.0"))
    assert DUAL_CONTROL in b["controls"]


def test_no_dual_control_for_low_tier():
    assert DUAL_CONTROL not in bundle(risk=RiskVerdict(tier="LOW"))["controls"]


def test_phase1_risk_is_not_classified():
    r = bundle()["risk"]
    assert r["tier"] == "not_classified" and r["classifier_version"] is None


def test_risk_records_fired_checks_and_versions():
    b = bundle(risk=RiskVerdict(tier="HIGH", classifier_version="1.2",
                                thresholds_version="2026-07-01",
                                checks_fired=({"check": "novel_zone_pair", "reason": "dmz->app"},)))
    assert b["risk"]["checks_fired"][0]["check"] == "novel_zone_pair"
    assert b["risk"]["thresholds_version"] == "2026-07-01"


# ── Failures are evidence too ─────────────────────────────────────────────
def test_failed_change_is_recorded():
    b = bundle(status="failed", failure_reason="push refused: unexpected staged changes")
    assert b["status"] == "failed"
    assert "unexpected staged changes" in b["failure"]["reason"]


def test_rejected_change_is_recorded():
    b = bundle(status="rejected", failure_reason="classifier: any-any to untrust")
    assert b["status"] == "rejected"


def test_failure_status_requires_a_reason():
    with pytest.raises(EvidenceError, match="failure_reason"):
        bundle(status="failed")


def test_invalid_status_rejected():
    with pytest.raises(EvidenceError, match="status must be"):
        bundle(status="probably-fine")


# ── Integrity guards ──────────────────────────────────────────────────────
def test_mismatched_intent_and_change_refused():
    ar, ch = pieces()
    other = valid_doc(); other["metadata"]["id"] = "REQ-DIFFERENT"
    other_ar = __import__("fwgitops.intent", fromlist=["load_intent"]).load_intent(other)
    with pytest.raises(EvidenceError, match="does not match compiled AccessRequest"):
        build_bundle(request=other_ar, compiled=ch, status="applied", generated_at=WHEN)


def test_no_credentials_leak_into_the_bundle():
    blob = dumps(bundle()).lower()
    for secret_ish in ("client_secret", "scm_client_secret", "password", "authorization", "bearer"):
        assert secret_ish not in blob


# ── Determinism + writing ─────────────────────────────────────────────────
def test_serialisation_is_byte_stable():
    assert dumps(bundle()) == dumps(bundle())
    json.loads(dumps(bundle()))


def test_write_bundle_path_layout(tmp_path):
    p = write_bundle(bundle(), tmp_path)
    assert p == tmp_path / "prod-edge" / "REQ-2026-0417.json"
    assert json.loads(p.read_text())["req_id"] == "REQ-2026-0417"


# ── CI context ────────────────────────────────────────────────────────────
def test_ci_context_from_github_env():
    ci = CIContext.from_env({
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_RUN_ID": "999",
        "GITHUB_SHA": "deadbeef",
    }, approvers=("bob@corp:deployment_gate",), gate="firewall-apply")
    assert ci.run_url == "https://github.com/org/repo/actions/runs/999"
    assert ci.merge_commit == "deadbeef"
    assert ci.approvers == (Approver(login="bob@corp", via="deployment_gate"),)
    assert ci.gate == "firewall-apply"


def test_ci_context_tolerates_missing_env():
    ci = CIContext.from_env({})
    assert ci.run_url is None and ci.merge_commit is None


# ── An unchanged change must not rewrite its record ────────────────────────
def _write(tmp_path, **kw):
    from fwgitops.evidence import write_bundle_if_changed
    return write_bundle_if_changed(bundle(**kw), tmp_path)


def test_an_unchanged_bundle_is_left_exactly_as_committed(tmp_path):
    """The misattribution this closes: every apply regenerated every bundle, and
    `generated_at` always moves, so the workflow committed all of them — each
    stamped with that run's `run_url` and `merge_commit`. A record for a request
    nobody touched claimed to have been applied by a run that applied something
    else."""
    from fwgitops.evidence import CIContext

    p, first = _write(tmp_path)
    before = p.read_bytes()

    # A LATER run: different time, different CI run, same change.
    _, second = _write(
        tmp_path,
        generated_at=datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
        ci=CIContext(pr_url="https://gh/pr/99", merge_commit="feedface",
                     run_url="https://gh/runs/1000", gate="firewall-apply"))
    assert first is True and second is False
    assert p.read_bytes() == before, "an untouched request must keep its own run"


def test_a_changed_spec_does_rewrite_the_record(tmp_path):
    """The other half — preserving must not become ignoring."""
    p, _ = _write(tmp_path)
    before = p.read_bytes()
    other = valid_doc()
    other["spec"]["source"] = [{"cidr": "10.0.0.0/8"}]     # widened
    ar2 = load_intent(other)
    _, written = __import__("fwgitops.evidence", fromlist=["x"]).write_bundle_if_changed(
        build_bundle(request=ar2, compiled=compile_request(ar2, env_map()),
                     status="applied", generated_at=WHEN), tmp_path)
    assert written is True
    assert p.read_bytes() != before


def test_a_new_status_rewrites_even_when_the_change_is_identical(tmp_path):
    """`applied` -> `failed` for the same config is a new OUTCOME, and failures
    are evidence too. Identity includes status precisely so this is not
    swallowed."""
    _write(tmp_path)
    p, written = _write(tmp_path, status="failed", failure_reason="push refused")
    assert written is True
    assert json.loads(p.read_text())["status"] == "failed"


def test_a_reclassification_alone_does_not_rewrite_the_record(tmp_path):
    """A later classifier re-tiering config nobody touched is policy drift, not a
    change. Backdating it into the record would claim this apply evaluated a
    ruleset that did not exist yet."""
    p, _ = _write(tmp_path)
    before = p.read_bytes()
    _, written = _write(tmp_path, risk=RiskVerdict(tier="CRITICAL", classifier_version="9.9"))
    assert written is False and p.read_bytes() == before


def test_the_object_hash_is_per_request_not_per_file(tmp_path):
    """`tfvars_sha256` is the whole FILE, and every rule in a folder shares
    `rules.auto.tfvars.json`. Keying identity on it would rewrite every rule's
    record whenever any neighbour changed — reintroducing the churn through the
    back door."""
    p, _ = _write(tmp_path)
    before = p.read_bytes()
    # A neighbouring request changed the shared file; THIS request did not.
    _, written = _write(tmp_path, tfvars_sha256=sha256_bytes(b"someone else changed it"))
    assert written is False and p.read_bytes() == before


def test_an_unreadable_existing_bundle_is_rewritten_not_preserved(tmp_path):
    p, _ = _write(tmp_path)
    p.write_text("{ truncated")
    _, written = _write(tmp_path)
    assert written is True
    assert json.loads(p.read_text())["req_id"] == "REQ-2026-0417"


# ── a control is CLAIMED only when it is EVIDENCED ────────────────────────
def test_CM5_is_not_claimed_without_a_named_approver():
    """The defect: `BASE_CONTROLS` listed CM-5 unconditionally while
    `CIContext.from_env` hard-coded `approvers=()` and `pr_url=None`, and no
    caller passed either. So every bundle ever written claimed "access
    restrictions for change" and named nobody. An assessor reads the claim, not
    the empty list beside it."""
    b = bundle(ci=CIContext(gate="firewall-apply"))
    assert APPROVAL_CONTROL not in b["controls"]
    gap = [g for g in b["controls_not_evidenced"] if g["control"] == APPROVAL_CONTROL]
    assert gap, "the omission must be NAMED — a shorter list reads as an older schema"
    assert "WHO approved" in gap[0]["why"]


def test_CM5_is_claimed_when_an_approver_is_named():
    b = bundle(ci=CIContext(gate="firewall-apply",
                            approvers=(Approver("alice", "deployment_gate"),)))
    assert APPROVAL_CONTROL in b["controls"]
    assert b["controls_not_evidenced"] == []


def test_a_protected_environment_ALONE_does_not_evidence_CM5():
    """`gate` is the environment's NAME. It says a restriction was configured,
    not that a human exercised it — and a required-reviewers rule that nobody has
    yet answered looks identical in the env var."""
    assert not CIContext(gate="firewall-apply").has_approval_evidence
    assert CIContext(gate="firewall-apply",
                     approvers=(Approver("a", "x"),)).has_approval_evidence


def test_the_approval_ROUTE_is_recorded_not_just_the_name():
    """Reviewing the proposed change and releasing the deployment are different
    acts. Collapsing them to a list of logins loses whether anyone actually
    exercised the deployment gate — and one person doing both is a finding."""
    b = bundle(ci=CIContext(approvers=(Approver("alice", "pull_request_review"),
                                       Approver("bob", "deployment_gate"))))
    assert b["approval"]["approvers"] == [
        {"login": "alice", "via": "pull_request_review"},
        {"login": "bob", "via": "deployment_gate"}]


def test_a_bare_login_is_recorded_as_unspecified_not_guessed():
    """Inventing which restriction was exercised would be the same class of
    fabrication as the ticket misattribution."""
    assert Approver.parse("alice").via == "unspecified"
    assert Approver.parse("alice:deployment_gate").via == "deployment_gate"


def test_approvers_are_coerced_however_the_context_is_built():
    """`from_env` is not the only door. A bare tuple of strings passed to the
    constructor used to survive until serialisation, failing far from the cause."""
    ci = CIContext(approvers=("alice:pull_request_review",))
    assert ci.approvers == (Approver("alice", "pull_request_review"),)


def test_pr_url_is_read_from_the_environment():
    """It was hard-coded to None, so no code path could ever have filled it."""
    assert CIContext.from_env({"GITHUB_PR_URL": "https://gh/pr/7"}).pr_url == "https://gh/pr/7"


# ── manual actions (ADR-0011) ──────────────────────────────────────────────

def test_a_manual_action_is_NOT_shaped_like_an_evidence_bundle():
    """An unmanaged object has no request id. Borrowing the bundle shape would
    imply an authorisation that never existed, so this record says plainly what
    it is: something done to SCM by hand, with who and why."""
    from fwgitops.evidence import MANUAL_ACTION_SCHEMA, build_manual_action

    r = build_manual_action(action="delete", kind="security-rule", folder="GitOps",
                            name="Testing-unmmanaged", object_id="abc",
                            tags=[], reason="unauthorised", actor="martono25",
                            run_url="https://example/run/1",
                            provenance="workflow", violation_id=None,
                            unlinked_reason="unit test")
    assert r["schema"] == MANUAL_ACTION_SCHEMA != "fw-evidence/v2"
    assert "req_id" not in r, "no request authorised this; do not imply one"
    for k in ("reason", "dispatched_by", "run_url", "tags_at_deletion", "object_id"):
        assert k in r, f"a manual action must record {k}"


def test_the_record_FILENAME_cannot_escape_its_directory(tmp_path):
    """SCM object names may contain slashes and spaces. The shipped version
    interpolated the name straight into the path, so a name with a slash wrote
    into a directory that does not exist — losing the record of an irreversible
    deletion, silently."""
    from fwgitops.evidence import build_manual_action, write_manual_action

    r = build_manual_action(action="delete", kind="address", folder="GitOps",
                            name="../../etc/passwd", object_id="x", tags=[],
                            reason="r", actor="a", run_url="u",
                            provenance="workflow", violation_id=None,
                            unlinked_reason="unit test", at="2026-08-16T05:00:00Z")
    out = write_manual_action(r, tmp_path)

    assert out.is_file()
    # THE RECORD LANDS DIRECTLY IN manual-actions/, nowhere else. Two weaker
    # assertions were tried first and both passed with the sanitisation
    # deleted: `tmp_path in out.parents` compares LEXICALLY and `..` is never
    # normalised, and even resolving it, `../..` from manual-actions/ lands at
    # tmp_path/etc/passwd.json — still inside the root, so "did it escape the
    # root" was the wrong question. "Is it where it belongs" is the right one.
    assert out.resolve().parent == (tmp_path / "manual-actions").resolve(), (
        f"the record was written outside its directory: {out.resolve()}")
    assert "/" not in out.name and ".." not in out.name
    # The RECORD still carries the true name — only the filename is sanitised.
    import json as _json
    assert _json.loads(out.read_text())["name"] == "../../etc/passwd"


def test_two_deletions_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    """Different objects, same timestamp — the name is part of the filename for
    exactly this reason."""
    from fwgitops.evidence import build_manual_action, write_manual_action

    paths = set()
    for n in ("rule-a", "rule-b"):
        r = build_manual_action(action="delete", kind="security-rule",
                                folder="GitOps", name=n, object_id="x", tags=[],
                                reason="r", actor="a", run_url="u",
                                provenance="workflow", violation_id=None,
                                unlinked_reason="unit test",
                                at="2026-08-16T05:00:00Z")
        paths.add(write_manual_action(r, tmp_path))
    assert len(paths) == 2


# ── provenance is STRUCTURAL, not a comment ─────────────────────────────────

def test_a_manual_action_must_SAY_how_it_came_to_exist():
    """The directory's whole value is that a machine wrote it.

    On 2026-08-16 a deletion succeeded and the record step crashed, so the only
    record of an irreversible act was rebuilt BY HAND from a CI log — and sat in
    this directory distinguished from a genuine record by a free-text field that
    nothing validated. Convention, not structure. A reader had to already know
    which field to look at, and to trust that whoever rebuilt it chose to fill
    it in.
    """
    from fwgitops.evidence import build_manual_action

    with pytest.raises(ValueError, match="provenance"):
        build_manual_action(action="delete", kind="address", folder="GitOps",
                            name="x", object_id="i", tags=[], reason="r",
                            actor="a", run_url="u", provenance="",
                            violation_id=None, unlinked_reason="t")


def test_provenance_has_NO_DEFAULT_so_a_reconstruction_cannot_pass_as_machine_written():
    """The design decision, pinned because it looks like an inconvenience.

    Defaulting to "workflow" would be worse than having no field: a
    reconstruction that simply forgot to say so would CLAIM machine authorship —
    the exact confusion this exists to prevent. With no default, the accidental
    path does not exist. It cannot stop someone stating it falsely, and makes no
    claim to; it removes the accident, not the lie.
    """
    import inspect

    from fwgitops.evidence import build_manual_action

    sig = inspect.signature(build_manual_action)
    prov = sig.parameters["provenance"]
    assert prov.default is inspect.Parameter.empty, (
        "provenance must be REQUIRED — a default would let a hand-written "
        "record silently claim the workflow wrote it")


def test_a_reconstruction_must_name_WHAT_it_was_rebuilt_from():
    """Otherwise it is an assertion wearing the word "reconstructed"."""
    from fwgitops.evidence import build_manual_action

    with pytest.raises(ValueError, match="rebuilt from"):
        build_manual_action(action="delete", kind="address", folder="GitOps",
                            name="x", object_id="i", tags=[], reason="r",
                            actor="a", run_url="u", provenance="reconstructed",
                            violation_id=None, unlinked_reason="t")

    ok = build_manual_action(action="delete", kind="address", folder="GitOps",
                             name="x", object_id="i", tags=[], reason="r",
                             actor="a", run_url="u", provenance="reconstructed",
                             violation_id=None, unlinked_reason="t",
                             reconstructed_from="https://example/run/9 log")
    assert ok["reconstructed_from"].startswith("https://")


def test_an_UNDECLARED_record_cannot_REACH_DISK(tmp_path):
    """Validation at write time, not left to the caller.

    A record that reaches disk has already become evidence — checking it
    afterwards is checking something that is already being relied on.
    """
    from fwgitops.evidence import build_manual_action, write_manual_action

    rec = build_manual_action(action="delete", kind="address", folder="GitOps",
                              name="x", object_id="i", tags=[], reason="r",
                              actor="a", run_url="u", provenance="workflow",
                              violation_id=None, unlinked_reason="unit test",
                              at="2026-08-16T05:00:00Z")
    del rec["provenance"]           # e.g. a v1 record from before the field
    with pytest.raises(ValueError, match="provenance"):
        write_manual_action(rec, tmp_path)
    assert not list(tmp_path.rglob("*.json")), "nothing may land un-validated"


def test_every_record_ON_DISK_declares_its_provenance():
    """The migration, pinned. Two v1 records predate the field; both were
    rewritten rather than grandfathered, because a directory where SOME records
    declare authorship and others do not is no better than one where none do."""
    from pathlib import Path

    from fwgitops.evidence import MANUAL_ACTION_SCHEMA, validate_manual_action

    root = Path(__file__).resolve().parents[1] / "evidence" / "manual-actions"
    found = sorted(root.glob("*.json"))
    assert found, "the committed records are the thing being checked"
    for f in found:
        rec = json.loads(f.read_text())
        assert rec["schema"] == MANUAL_ACTION_SCHEMA, f"{f.name} was not migrated"
        validate_manual_action(rec)


def test_a_deletion_must_SAY_WHICH_violation_it_remediates():
    """The link is the whole point of giving violations an id.

    Until 2026-08-16 the connection between an unauthorised change and the act
    that removed it existed only as prose someone typed into `reason`. Nothing
    could join the two directories, so "show me every unauthorised change and
    what remediated it" was a manual read. On the record it is a query.
    """
    from fwgitops.evidence import build_manual_action

    rec = build_manual_action(action="delete", kind="security-rule",
                              folder="GitOps", name="x", object_id="i", tags=[],
                              reason="r", actor="a", run_url="u",
                              provenance="workflow",
                              violation_id="VIOL-2026-0816-GitOps-x")
    assert rec["violation_id"] == "VIOL-2026-0816-GitOps-x"
    assert "unlinked_reason" not in rec, "nothing to explain when it IS linked"


def test_a_deletion_that_remediates_NOTHING_must_say_so_out_loud():
    """Deleting something the platform never flagged is legitimate — both test
    fixtures were created and destroyed between drift runs. It is also exactly
    the case worth stating, so an empty field cannot pass as an oversight."""
    from fwgitops.evidence import build_manual_action

    with pytest.raises(ValueError, match="unlinked_reason"):
        build_manual_action(action="delete", kind="address", folder="GitOps",
                            name="f", object_id="i", tags=[], reason="r",
                            actor="a", run_url="u", provenance="workflow",
                            violation_id=None)


def test_a_link_that_resolves_to_NOTHING_is_refused():
    """A plausible-looking id nobody can look up is worse than no id: it reads
    as provenance while pointing nowhere."""
    from fwgitops.evidence import build_manual_action

    with pytest.raises(ValueError, match="violation id"):
        build_manual_action(action="delete", kind="address", folder="GitOps",
                            name="f", object_id="i", tags=[], reason="r",
                            actor="a", run_url="u", provenance="workflow",
                            violation_id="see the ticket")


def test_every_committed_deletion_resolves_to_a_REAL_violation_or_declares_none():
    """The committed evidence, joined. This is the query the id exists to make
    possible, run against what is actually on disk."""
    import glob
    from pathlib import Path

    from fwgitops.evidence import validate_manual_action

    root = Path(__file__).resolve().parents[1] / "evidence"
    known = {json.loads(p.read_text())["id"]
             for p in (root / "violations").glob("*.json")}

    actions = sorted((root / "manual-actions").glob("*.json"))
    assert actions, "the committed records are the thing being checked"
    for a in actions:
        rec = json.loads(a.read_text())
        validate_manual_action(rec)
        if rec["violation_id"] is not None:
            assert rec["violation_id"] in known, (
                f"{a.name} remediates {rec['violation_id']}, which no violation "
                f"record claims — a dangling link is worse than none")


def test_the_version_stamped_into_evidence_MATCHES_the_package():
    """Every bundle records `compiled.compiler_version`, and it was a lie.

    `__init__.py` hardcoded "1.0.0" while pyproject said 2.3.0 and the repo sat
    139 commits past the v2.3.0 tag. Every audit record on `main` therefore
    claimed it was produced by a version that shipped months earlier — and an
    assessor tracing a defect to the tool that made it would have been sent to
    the wrong code.

    Two constants for one fact drift the moment somebody bumps the one they can
    see, so the package reads its own installed metadata and this pins that they
    agree.
    """
    import re
    from pathlib import Path

    import fwgitops

    # A REGEX, NOT `tomllib`: that module is 3.11+, and this project's own venv
    # runs 3.9 while CI runs 3.11. A test that passes on one interpreter and
    # ImportErrors on the other is worse than no test — it goes green where
    # nobody looks and red where somebody is already busy.
    root = Path(__file__).resolve().parents[1]
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (root / "pyproject.toml").read_text(), re.M)
    assert m, "pyproject.toml declares no version"
    declared = m.group(1)
    assert fwgitops.__version__ == declared, (
        f"fwgitops.__version__ is {fwgitops.__version__!r} but pyproject declares "
        f"{declared!r}; evidence bundles stamp the former as compiler_version")
