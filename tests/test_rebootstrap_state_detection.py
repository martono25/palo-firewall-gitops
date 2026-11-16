"""Archiving a run-once root's state when it belongs to another account.

MEASURED 2026-11-15, mid-move. `rebootstrap-account.sh` decided which account a
local state file belonged to by grepping it for an IAM ARN. `github-oidc` state
holds ARNs and archived correctly; `bootstrap-backend` state holds none — only
the bucket name — so the check found nothing, printed "state already belongs to
<the new account>", and terraform then refreshed the dead account's bucket:

    Error: reading S3 Bucket Versioning (fw-gitops-tfstate-975239810400):
    403 AllAccessDisabled

The reassurance was the defect: it asserted membership it had never checked.
These tests run the SHIPPED shell, not a copy of it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "terraform" / "lib" / "archive-foreign-state.sh"
NEW = "111122223333"
OLD = "975239810400"


def _run(dir_: Path, acct: str = NEW) -> str:
    out = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; archive_foreign_state "{dir_}" "{acct}"'],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _state(dir_: Path, body: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    f = dir_ / "terraform.tfstate"
    f.write_text(json.dumps(body))
    (dir_ / "terraform.tfstate.backup").write_text("{}")
    return f


def test_the_BUCKET_ONLY_state_is_archived(tmp_path):
    """THE DEFECT. bootstrap-backend state names the account only inside the
    bucket name. The old ARN grep saw nothing and kept it."""
    d = tmp_path / "bootstrap-backend"
    _state(d, {"resources": [{"type": "aws_s3_bucket",
                              "instances": [{"attributes": {
                                  "bucket": f"fw-gitops-tfstate-{OLD}"}}]}]})
    msg = _run(d)
    assert "does NOT name" in msg and OLD in msg
    assert not (d / "terraform.tfstate").exists()
    assert (d / f".old-account-{OLD}" / "terraform.tfstate").is_file()
    assert (d / f".old-account-{OLD}" / "terraform.tfstate.backup").is_file(), (
        "the .backup must travel with it, or terraform can still read it")


def test_state_naming_an_IAM_ARN_is_archived_too(tmp_path):
    """The case that already worked — it must keep working."""
    d = tmp_path / "github-oidc"
    _state(d, {"resources": [{"attributes": {"arn": f"arn:aws:iam::{OLD}:role/x"}}]})
    assert OLD in _run(d)
    assert (d / f".old-account-{OLD}" / "terraform.tfstate").is_file()


def test_state_that_NAMES_THE_CURRENT_ACCOUNT_is_kept(tmp_path):
    """The guard must not become "archive everything": a re-run in the SAME
    account has to keep its state, or terraform would try to create a bucket
    that already exists."""
    d = tmp_path / "bootstrap-backend"
    _state(d, {"resources": [{"instances": [{"attributes": {
        "bucket": f"fw-gitops-tfstate-{NEW}"}}]}]})
    assert "kept" in _run(d)
    assert (d / "terraform.tfstate").is_file()


def test_state_naming_NO_account_at_all_is_archived_as_unknown(tmp_path):
    """FAIL CLOSED. State we cannot attribute is the case that caused this: the
    old code treated "cannot tell" as "belongs to us"."""
    d = tmp_path / "bootstrap-backend"
    _state(d, {"resources": [], "serial": 3})
    msg = _run(d)
    assert "unknown" in msg
    assert (d / ".old-account-unknown" / "terraform.tfstate").is_file()


def test_no_state_is_not_an_error(tmp_path):
    d = tmp_path / "github-oidc"
    d.mkdir()
    assert _run(d) == "no local state"


def test_the_script_USES_this_helper_rather_than_its_own_grep():
    """Two implementations would drift, and the inline one is the one that was
    wrong."""
    script = (LIB.parents[1] / "rebootstrap-account.sh").read_text()
    assert "archive-foreign-state.sh" in script, "the script must source the helper"
    assert "arn:aws:iam::[0-9]{12}" not in script, (
        "the ARN-only grep is the defect — it must not survive in the script")
    assert "state already belongs to" not in script, (
        "that line asserted membership it never checked")
