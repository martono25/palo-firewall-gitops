"""Credential redaction for published CI artifacts.

GitHub masks secrets in the live log stream. It does NOT mask them in uploaded
artifact contents or in `gh pr comment` bodies — and pr-validate publishes both,
from files that capture terraform's stderr while SCM_CLIENT_SECRET is in the job
env. This is the guard for that, so it needs tests: a redaction script nobody
checks is the same category of thing as a gitignore rule nobody checks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "redact.py"


def _load():
    spec = importlib.util.spec_from_file_location("redact", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redact_mod = _load()


def test_the_script_exists_where_the_workflow_calls_it():
    """The workflow invokes it by path; a rename would break publishing silently
    (the step fails, but only after the plan step has already written the file)."""
    assert SCRIPT.is_file()
    wf = (REPO_ROOT / ".github" / "workflows" / "pr-validate.yml").read_text()
    assert ".github/scripts/redact.py" in wf


def test_a_secret_is_removed():
    out, hits = redact_mod.redact("token=sup3rs3cr3t-abcdef done", ["sup3rs3cr3t-abcdef"])
    assert hits == 1 and "sup3rs3cr3t" not in out and "***REDACTED***" in out


def test_every_occurrence_is_removed_not_just_the_first():
    text = "a SEKRIT-value-here b SEKRIT-value-here c"
    out, hits = redact_mod.redact(text, ["SEKRIT-value-here"])
    assert hits == 2 and "SEKRIT" not in out


def test_unrelated_content_survives():
    """Redaction must not corrupt the plan output it is protecting."""
    text = "Plan: 3 to add, 0 to change.\nauth=SEKRIT-value-here\n"
    out, _ = redact_mod.redact(text, ["SEKRIT-value-here"])
    assert "Plan: 3 to add, 0 to change." in out


def test_multiple_secrets_are_all_removed():
    text = "id=svc@1198884949.iam.panserviceaccount.com secret=abcdefgh12345678"
    out, hits = redact_mod.redact(
        text, ["svc@1198884949.iam.panserviceaccount.com", "abcdefgh12345678"])
    assert hits == 2 and "panserviceaccount" not in out and "abcdefgh" not in out


def test_substrings_with_regex_metacharacters_are_handled_literally():
    """A secret can contain any character. Building a regex from one risks both
    mis-escaping and a wrong match — this is literal replacement."""
    secret = "a+b*c?d[e]f.g$h"
    out, hits = redact_mod.redact(f"value={secret} end", [secret])
    assert hits == 1 and secret not in out and "end" in out


def test_the_env_var_list_covers_every_secret_the_workflow_injects():
    """A secret in the job env but not in SECRET_VARS would pass through
    unredacted — the exact gap this guard exists to close."""
    wf = (REPO_ROOT / ".github" / "workflows" / "pr-validate.yml").read_text()
    injected = {
        line.split(":")[0].strip()
        for line in wf.splitlines()
        if "${{ secrets." in line or "${{ github.token }}" in line
    }
    missing = injected - set(redact_mod.SECRET_VARS)
    assert not missing, f"injected into the job env but never redacted: {sorted(missing)}"


@pytest.mark.parametrize("short", ["", "x", "short"])
def test_short_values_are_ignored(short, tmp_path, monkeypatch):
    """Replacing a 1-char 'secret' would corrupt text without protecting
    anything — a placeholder or unset var must not mangle the artifact."""
    f = tmp_path / "plan.txt"
    f.write_text("Plan: 3 to add, 0 to change.\n")
    monkeypatch.setenv("SCM_CLIENT_SECRET", short)
    for name in redact_mod.SECRET_VARS:
        if name != "SCM_CLIENT_SECRET":
            monkeypatch.delenv(name, raising=False)
    redact_mod.main([str(f)])
    assert f.read_text() == "Plan: 3 to add, 0 to change.\n"


def test_end_to_end_scrubs_a_file_in_place(tmp_path, monkeypatch):
    f = tmp_path / "plan-prod-edge.txt"
    f.write_text("Error: auth failed using abcdefgh12345678\nPlan: 1 to add.\n")
    monkeypatch.setenv("SCM_CLIENT_SECRET", "abcdefgh12345678")
    redact_mod.main([str(f)])
    got = f.read_text()
    assert "abcdefgh12345678" not in got and "Plan: 1 to add." in got


def test_a_missing_file_is_not_an_error(tmp_path):
    """The workflow globs plan-*.txt; a folder that produced none is normal."""
    assert redact_mod.main([str(tmp_path / "nope.txt")]) == 0
