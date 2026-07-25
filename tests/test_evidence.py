"""Tests for the NIST-mapped evidence bundle."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fwgitops.compiler import compile_request
from fwgitops.evidence import (
    DUAL_CONTROL,
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
        request=ar, change=ch, status="applied", generated_at=WHEN,
        intent_sha256=sha256_bytes(b"intent"), intent_path="intent/prod/x.yaml",
        tfvars_sha256=sha256_bytes(b"tfvars"), plan_sha256=sha256_bytes(b"plan"),
        ci=CIContext(pr_url="https://gh/pr/42", merge_commit="abc123",
                     run_url="https://gh/runs/9", gate="firewall-apply",
                     approvers=("alice@corp",)),
        push=PushResult(folder="prod-edge", status="success", job_id="job-1",
                        editors=("GitOps@1198884949.iam.panserviceaccount.com",)),
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
    assert r["expires"] == "2026-10-19"
    assert len(r["intent_sha256"]) == 64          # hash, not a copy


def test_compiled_section_records_what_was_built():
    c = bundle()["compiled"]
    assert c["folder"] == "prod-edge"
    assert c["rule"]["from_zones"] == ["trust"] and c["rule"]["to_zones"] == ["app"]
    assert c["rule"]["action"] == "allow"
    assert any(t.startswith("gitops:req:") for t in c["tags"])
    assert c["compiler_version"]                  # reproducibility
    assert len(c["tfvars_sha256"]) == 64


def test_approval_and_apply_sections():
    b = bundle()
    assert b["approval"]["approvers"] == ["alice@corp"]
    assert b["approval"]["pr"] == "https://gh/pr/42"
    assert b["approval"]["merge_commit"] == "abc123"
    assert b["apply"]["run_url"] == "https://gh/runs/9"


def test_push_section_from_push_result():
    assert bundle()["push"]["status"] == "success"
    assert bundle()["push"]["job_id"] == "job-1"


# ── Controls ──────────────────────────────────────────────────────────────
def test_base_controls_present():
    assert set(bundle()["controls"]) >= {"AC-4", "CM-3", "CM-5", "AU-2", "AU-12", "SC-7"}


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
    with pytest.raises(EvidenceError, match="does not match compiled rule"):
        build_bundle(request=other_ar, change=ch, status="applied", generated_at=WHEN)


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
    }, approvers=("bob@corp",), gate="firewall-apply")
    assert ci.run_url == "https://github.com/org/repo/actions/runs/999"
    assert ci.merge_commit == "deadbeef"
    assert ci.approvers == ("bob@corp",)
    assert ci.gate == "firewall-apply"


def test_ci_context_tolerates_missing_env():
    ci = CIContext.from_env({})
    assert ci.run_url is None and ci.merge_commit is None
