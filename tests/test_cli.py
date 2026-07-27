"""Tests for the CLI compile command (reads real YAML on disk)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fwgitops.cli import run_compile

VALID_INTENT = """\
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0417
  requester: jane.doe@corp
  ticket: JIRA-12345
  justification: "Web tier needs to reach the payments API"
  requested: 2026-07-19
  expires: 2026-10-19
spec:
  environment: prod
  action: allow
  source:
    - cidr: 10.20.1.0/24
  destination:
    - cidr: 10.20.9.10/32
  service:
    - protocol: tcp
      port: 443
  log: true
"""

ENV_MAP = "prod:\n  folder: prod-edge\n  from_zone: trust\n  to_zone: app\n"


def _setup(tmp_path: Path, intent_body: str = VALID_INTENT, name: str = "REQ.yaml") -> tuple:
    intent_root = tmp_path / "intent" / "prod"
    intent_root.mkdir(parents=True)
    (intent_root / name).write_text(intent_body)
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"
    return tmp_path / "intent", env_map, out


def test_compile_writes_tfvars(tmp_path, capsys):
    intent_root, env_map, out = _setup(tmp_path)
    rc = run_compile(intent_root, env_map, out)
    assert rc == 0
    target = out / "prod-edge" / "rules.auto.tfvars.json"
    assert target.is_file()
    data = json.loads(target.read_text())
    assert "REQ-2026-0417" in data["security_rules"]
    assert data["security_rules"]["REQ-2026-0417"]["folder"] == "prod-edge"


def test_check_mode_writes_nothing(tmp_path):
    intent_root, env_map, out = _setup(tmp_path)
    rc = run_compile(intent_root, env_map, out, write=False)
    assert rc == 0
    assert not (out / "prod-edge" / "rules.auto.tfvars.json").exists()


def test_invalid_intent_exits_2_and_writes_nothing(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: permit")
    intent_root, env_map, out = _setup(tmp_path, intent_body=bad)
    rc = run_compile(intent_root, env_map, out)
    assert rc == 2
    assert not out.exists()  # all-or-nothing: nothing written on failure
    err = capsys.readouterr().err
    assert "REJECTED" in err and "spec.action" in err


def test_unknown_environment_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("environment: prod", "environment: staging")
    intent_root, env_map, out = _setup(tmp_path, intent_body=bad)
    rc = run_compile(intent_root, env_map, out)
    assert rc == 2
    assert "staging" in capsys.readouterr().err


def test_one_bad_file_blocks_the_whole_run(tmp_path):
    # A valid + an invalid intent: the run rejects, writes nothing (all-or-nothing).
    intent_root, env_map, out = _setup(tmp_path, name="good.yaml")
    (intent_root / "prod" / "bad.yaml").write_text(
        VALID_INTENT.replace("id: REQ-2026-0417", "id: REQ-2026-0500").replace(
            "port: 443", "port: 70000"
        )
    )
    rc = run_compile(intent_root, env_map, out)
    assert rc == 2
    assert not out.exists()


def test_example_files_are_skipped(tmp_path):
    intent_root, env_map, out = _setup(tmp_path)
    # An .example. file that is intentionally not valid Phase-1 intent.
    (intent_root / "prod" / "REQ.example.yaml").write_text("apiVersion: nope\n")
    rc = run_compile(intent_root, env_map, out)
    assert rc == 0  # example ignored, valid intent still compiles


def test_no_intents_is_ok(tmp_path, capsys):
    (tmp_path / "intent").mkdir()
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    rc = run_compile(tmp_path / "intent", env_map, tmp_path / "terraform")
    assert rc == 0
    assert "no intent files" in capsys.readouterr().out


def test_missing_env_map_exits_1(tmp_path, capsys):
    (tmp_path / "intent").mkdir()
    rc = run_compile(tmp_path / "intent", tmp_path / "nope.yaml", tmp_path / "terraform")
    assert rc == 1
    assert "env map not found" in capsys.readouterr().err


# ── fwgitops classify (Phase 2) ────────────────────────────────────────────
from fwgitops.cli import run_classify  # noqa: E402


def test_classify_reports_tier(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    rc = run_classify(intent_root, env_map)  # report-only -> exit 0 whatever the tier
    assert rc == 0
    o = capsys.readouterr().out
    assert "REQ-2026-0417" in o and "classified 1" in o


def _second_intent_same_zone_pair(intent_root):
    # A second prod intent (same trust->app zone-pair, different source) so
    # neither rule is a "novel zone-pair" -> both classify on stateless merits.
    body = VALID_INTENT.replace("REQ-2026-0417", "REQ-2026-0999").replace(
        "10.20.1.0/24", "10.20.5.0/24")
    (intent_root / "prod" / "REQ2.yaml").write_text(body)


def test_classify_rejects_invalid_intent_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: permit")
    intent_root, env_map, _ = _setup(tmp_path, intent_body=bad)
    rc = run_classify(intent_root, env_map)
    assert rc == 2


_BROAD = VALID_INTENT.replace("cidr: 10.20.1.0/24", "cidr: 10.0.0.0/8")  # broad_source -> HIGH


def test_classify_gate_blocks_change_above_tier(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path, intent_body=_BROAD)
    rc = run_classify(intent_root, env_map, gate="LOW")
    assert rc == 3
    assert "GATE" in capsys.readouterr().err


def test_classify_gate_allows_low_change(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    _second_intent_same_zone_pair(intent_root)  # shared zone-pair -> not novel -> LOW
    assert run_classify(intent_root, env_map, gate="LOW") == 0


def test_classify_gate_higher_override_allows_high(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path, intent_body=_BROAD)
    assert run_classify(intent_root, env_map, gate="CRITICAL") == 0  # explicit override


# ── fwgitops evidence (Phase 2) ────────────────────────────────────────────
from fwgitops.cli import run_evidence  # noqa: E402


def test_evidence_writes_bundle_with_risk_and_provenance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    intent_root, env_map, _ = _setup(tmp_path)
    out = tmp_path / "ev"
    rc = run_evidence(intent_root, env_map, out, tfvars_root=tmp_path / "none")
    assert rc == 0
    b = json.loads((out / "prod-edge" / "REQ-2026-0417.json").read_text())
    assert b["schema"] == "fw-evidence/v1" and b["status"] == "applied"
    assert b["risk"]["classifier_version"]                    # risk verdict recorded
    assert b["apply"]["run_url"].endswith("/actions/runs/42")  # CI provenance
    assert set(b["controls"]) >= {"AC-4", "CM-3", "AU-12"}


def test_evidence_rejects_invalid_intent_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: nope")
    intent_root, env_map, _ = _setup(tmp_path, intent_body=bad)
    assert run_evidence(intent_root, env_map, tmp_path / "ev") == 2


# ── fwgitops drift (Phase 2) ───────────────────────────────────────────────
from fwgitops.cli import run_drift  # noqa: E402

_MANAGED_417 = ["gitops:managed", "gitops:req:REQ-2026-0417"]


def test_drift_flags_unmanaged_exit_3(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)  # declares REQ-2026-0417 in prod-edge
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps([
        {"folder": "prod-edge", "name": "REQ-2026-0417", "tags": _MANAGED_417},
        {"folder": "prod-edge", "name": "MANUAL-RULE", "tags": []},
    ]))
    rc = run_drift(intent_root, env_map, snap)
    assert rc == 3
    assert "MANUAL-RULE" in capsys.readouterr().out


def test_drift_clean_exit_0(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps([{"folder": "prod-edge", "name": "REQ-2026-0417", "tags": _MANAGED_417}]))
    assert run_drift(intent_root, env_map, snap) == 0


def test_drift_missing_snapshot_exit_1(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    assert run_drift(intent_root, env_map, tmp_path / "nope.json") == 1


# ── fwgitops push (T13) ────────────────────────────────────────────────────
from fwgitops.cli import run_push  # noqa: E402
from fwgitops.scmapi import ScmCredentials, ScmSession  # noqa: E402

SA = "GitOps@1198884949.iam.panserviceaccount.com"
CREDS = ScmCredentials(client_id=SA, client_secret="s3cret", scope="tsg_id:1198884949")


def _scm_transport(nothing_to_push=False, sink=None):
    def t(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        if "candidate:push" in url:                         # POST push
            if sink is not None:
                sink.append(json.loads(body))
            if nothing_to_push:
                return 400, json.dumps({"message": "no changes to push"}).encode()
            return 200, json.dumps({"job_id": "job-9"}).encode()
        if "/jobs/" in url:                                 # GET job status
            return 200, json.dumps({"data": [{"status_str": "FIN", "result_str": "OK"}]}).encode()
        return 404, b"{}"
    return t


def _session(**kw):
    return ScmSession(CREDS, transport=_scm_transport(**kw))


def test_cli_push_success_scoped_to_service_account(capsys):
    sink = []
    rc = run_push("GitOps", session=ScmSession(CREDS, transport=_scm_transport(sink=sink)))
    assert rc == 0
    assert "OK — success" in capsys.readouterr().out
    assert sink[-1]["admin"] == [SA]          # default: commit only our SA's changes


def test_cli_push_all_admins_is_unscoped(capsys):
    sink = []
    rc = run_push("GitOps", all_admins=True,
                  session=ScmSession(CREDS, transport=_scm_transport(sink=sink)))
    assert rc == 0
    assert "admin" not in sink[-1]            # break-glass: whole candidate


def test_cli_push_noop_when_nothing_staged(capsys):
    rc = run_push("GitOps", session=_session(nothing_to_push=True))
    assert rc == 0
    assert "noop" in capsys.readouterr().out


def test_cli_push_missing_env_exits_1(monkeypatch, capsys):
    for v in ("SCM_CLIENT_ID", "SCM_CLIENT_SECRET", "SCM_SCOPE"):
        monkeypatch.delenv(v, raising=False)
    rc = run_push("GitOps")   # no session -> from_env -> missing -> 1
    assert rc == 1


from fwgitops.cli import run_set_admin_password  # noqa: E402


def test_cli_set_admin_password_missing_phash_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("FWGITOPS_ADMIN_PHASH", raising=False)
    rc = run_set_admin_password("10.0.0.1", ssh_key="k.pem")  # no phash -> 1, no SSH
    assert rc == 1
    assert "FWGITOPS_ADMIN_PHASH" in capsys.readouterr().err
