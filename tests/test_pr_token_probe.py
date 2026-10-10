"""An EXPIRED `AUTOMATION_PR_TOKEN` must fall back, not fail.

Every workflow that opens a PR used `secrets.AUTOMATION_PR_TOKEN || github.token`.
That falls back only when the secret is UNSET. A fine-grained PAT that has
expired or been revoked is still set, so each use kept sending it: the push or
`gh pr create` fails, and the evidence, violation or remediation record never
lands. The "not set" warning — the only signal written for this — never fires,
because the secret is not empty.

These tests run the SHELL each workflow actually ships, extracted from the YAML,
against a stub `curl` — so what is tested is what runs, not a copy of it.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SECRET = "secrets.AUTOMATION_PR_TOKEN"
GUARDED = "steps.pat.outputs.ok == 'true' && secrets.AUTOMATION_PR_TOKEN || github.token"


def _jobs_using_the_token():
    out = []
    for f in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(f.read_text())
        for job_name, job in (wf.get("jobs") or {}).items():
            if SECRET in yaml.safe_dump(job):
                out.append((f.name, job_name, job))
    return out


def _probe(job):
    steps = job.get("steps") or []
    return next((s for s in steps if s.get("id") == "pat"), None)


JOBS = _jobs_using_the_token()


def test_the_token_is_used_somewhere():
    """Guards the discovery: a glob matching nothing makes every test below pass."""
    assert {name for name, _, _ in JOBS} >= {
        "apply.yml", "intake.yml", "drift-detect.yml", "remediate.yml",
        "delete-scm-object.yml"}


@pytest.mark.parametrize("wf,job_name,job", JOBS, ids=lambda x: x if isinstance(x, str) else "")
def test_every_job_that_uses_the_token_PROBES_it_FIRST(wf, job_name, job):
    """First, not merely earlier: the checkout's token is the one `git push`
    uses, so a probe placed after checkout would be too late for it."""
    steps = job.get("steps") or []
    assert steps and steps[0].get("id") == "pat", (
        f"{wf}:{job_name} must run the token probe as its first step")


@pytest.mark.parametrize("wf,job_name,job", JOBS, ids=lambda x: x if isinstance(x, str) else "")
def test_no_use_of_the_token_skips_the_probe(wf, job_name, job):
    """The probe is worthless if one use still reads the secret directly — the
    unguarded one is the push or PR that fails on expiry."""
    for step in job.get("steps") or []:
        if step.get("id") == "pat":
            continue
        for where in ("with", "env"):
            for key, value in (step.get(where) or {}).items():
                v = str(value)
                if SECRET in v and "!= ''" not in v:
                    assert GUARDED in v, (
                        f"{wf}:{job_name} step {step.get('name') or step.get('uses')!r} "
                        f"{where}.{key} uses the token without the probe: {v}")
                assert "secrets.AUTOMATION_PR_TOKEN != ''" not in v, (
                    f"{wf}:{job_name} tests the secret for EMPTINESS — an expired "
                    f"token is not empty. Use steps.pat.outputs.ok")


def test_the_probe_is_IDENTICAL_everywhere():
    """Five copies exist because it must run before checkout, which rules out a
    local composite action. Identical is what makes five copies one thing."""
    bodies = {(wf, job): _probe(j)["run"] for wf, job, j in JOBS}
    assert len(set(bodies.values())) == 1, sorted(bodies)


# ── the probe, executed ─────────────────────────────────────────────────────

def _run_probe(tmp_path, pat, repo_code, pulls_code):
    """Run the shipped probe with a stub curl answering the two calls."""
    body = _probe(JOBS[0][2])["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    # The stub sees the URL as its last argument; `/pulls` decides which answer.
    curl.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do last="$a"; done\n'
        'case "$last" in *"/pulls"*) printf "%s" "$PULLS";; *) printf "%s" "$REPO";; esac\n'
        '[ "$FAIL" = 1 ] && exit 7\nexit 0\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "github_output"
    out.write_text("")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PAT": pat,
           "GITHUB_OUTPUT": str(out), "GITHUB_API_URL": "https://api.github.com",
           "GITHUB_REPOSITORY": "o/r", "REPO": repo_code, "PULLS": pulls_code,
           "FAIL": "1" if repo_code == "000" else "0"}
    proc = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True)
    return proc, out.read_text()


def test_a_USABLE_token_is_used(tmp_path):
    proc, out = _run_probe(tmp_path, "github_pat_x", "200", "200")
    assert proc.returncode == 0 and "ok=true" in out
    assert "::warning::" not in proc.stdout


def test_an_EXPIRED_token_falls_back_and_SAYS_SO(tmp_path):
    """The case this exists for: set, but GitHub answers 401."""
    proc, out = _run_probe(tmp_path, "github_pat_expired", "401", "401")
    assert proc.returncode == 0, "the probe must never fail the job"
    assert "ok=false" in out
    assert "SET but GitHub rejected it" in proc.stdout and "401" in proc.stdout


def test_a_token_WITHOUT_pull_request_access_falls_back(tmp_path):
    """Authenticates, cannot read pulls — PR authorship would fail at create."""
    proc, out = _run_probe(tmp_path, "github_pat_narrow", "200", "403")
    assert "ok=false" in out and "pulls HTTP 403" in proc.stdout


def test_an_UNSET_token_falls_back_with_its_own_message(tmp_path):
    proc, out = _run_probe(tmp_path, "", "200", "200")
    assert "ok=false" in out and "is not set" in proc.stdout
    assert "rejected" not in proc.stdout, "unset and expired need different fixes"


def test_an_UNREACHABLE_api_falls_back_and_does_not_fail_the_job(tmp_path):
    """curl failing outright must not turn a recoverable degradation into a
    failed run — that would lose the record this protects."""
    proc, out = _run_probe(tmp_path, "github_pat_x", "000", "000")
    assert proc.returncode == 0 and "ok=false" in out
