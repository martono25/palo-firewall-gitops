"""Tests for the post-boot admin-password step (route B)."""

from __future__ import annotations

import pytest

from fwgitops.admin_password import AdminPasswordError, set_admin_phash

PHASH = "$5$ciwuqorx$OnAXHUD..CKUOj0y3RFgVUfJGW091h2S/iq3P7jXKaC"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def runner_returning(returncode=0, stdout="", stderr=""):
    captured = {}

    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        captured["argv"] = argv
        captured["input"] = input
        return FakeProc(returncode, stdout, stderr)

    run.captured = captured
    return run


OK = "Commit job 42 ... Configuration committed successfully"


def test_sets_phash_via_stdin_and_commits():
    r = runner_returning(stdout=OK)
    set_admin_phash("10.0.0.1", PHASH, ssh_key="k.pem", runner=r)
    script = r.captured["input"]
    assert "set mgt-config users admin phash" in script
    assert PHASH in script and "commit" in script
    # the hash is fed over stdin, never on the command line (argv/ps leak)
    assert PHASH not in " ".join(r.captured["argv"])


def test_rejects_plaintext_password():
    with pytest.raises(AdminPasswordError, match="not a crypt hash"):
        set_admin_phash("10.0.0.1", "Fw!Prod2026", ssh_key="k.pem", runner=runner_returning())


def test_ssh_failure_raises():
    r = runner_returning(returncode=255, stderr="Permission denied (publickey)")
    with pytest.raises(AdminPasswordError, match="failed"):
        set_admin_phash("10.0.0.1", PHASH, ssh_key="k.pem", runner=r)


def test_commit_not_confirmed_fails_closed():
    # rc 0 but no success line -> never assume the commit worked.
    r = runner_returning(stdout="configure\n[edit]\n")
    with pytest.raises(AdminPasswordError, match="could not confirm"):
        set_admin_phash("10.0.0.1", PHASH, ssh_key="k.pem", runner=r)


def test_custom_user_targets_that_account():
    r = runner_returning(stdout=OK)
    set_admin_phash("10.0.0.1", PHASH, ssh_key="k.pem", user="ops", runner=r)
    assert "set mgt-config users ops phash" in r.captured["input"]
    assert "ops@10.0.0.1" in r.captured["argv"]
