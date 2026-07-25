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


# ── fwgitops push (T13) ────────────────────────────────────────────────────
from fwgitops.cli import run_push  # noqa: E402
from fwgitops.scmapi import ScmCredentials, ScmSession  # noqa: E402

SA = "GitOps@1198884949.iam.panserviceaccount.com"
CREDS = ScmCredentials(client_id=SA, client_secret="s3cret", scope="tsg_id:1198884949")


def _scm_transport(editors):
    def t(method, url, headers, body):
        if "oauth2" in url:
            payload = {"access_token": "tok", "expires_in": 3600}
        elif "candidate:push" in url:                      # POST push — check first
            payload = {"job_id": "job-9"}
        elif "config-versions/candidate" in url:            # GET candidate editors
            payload = {"data": [{"edited_by": e} for e in editors]}
        elif "/jobs/" in url:                               # GET job status
            payload = {"data": [{"status_str": "FIN", "result_str": "OK"}]}
        else:
            return 404, b"{}"
        return 200, json.dumps(payload).encode()
    return t


def _session(editors):
    return ScmSession(CREDS, transport=_scm_transport(editors))


def test_cli_push_success(capsys):
    rc = run_push("GitOps", session=_session([SA]))
    assert rc == 0
    assert "OK — success" in capsys.readouterr().out


def test_cli_push_refused_on_outside_editor(capsys):
    rc = run_push("GitOps", session=_session([SA, "human@corp"]))
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "human@corp" in err


def test_cli_push_break_glass_overrides(capsys):
    rc = run_push("GitOps", session=_session([SA, "human@corp"]), allow_unexpected=True)
    assert rc == 0


def test_cli_push_noop_when_nothing_staged(capsys):
    rc = run_push("GitOps", session=_session([]))
    assert rc == 0
    assert "noop" in capsys.readouterr().out


def test_cli_push_missing_env_exits_1(monkeypatch, capsys):
    for v in ("SCM_CLIENT_ID", "SCM_CLIENT_SECRET", "SCM_SCOPE"):
        monkeypatch.delenv(v, raising=False)
    rc = run_push("GitOps")   # no session -> from_env -> missing -> 1
    assert rc == 1
