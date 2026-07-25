"""Tests for the SCM push step (T13) — the atomic commit boundary.

The fail-closed guard is the security-critical behavior: a folder-scoped push
commits everything staged in the candidate, so pushing when someone OUTSIDE our
automation has edited the candidate would ship their unreviewed change under our
audit trail. The guard keys on WHO edited the candidate (pilot finding).
"""

from __future__ import annotations

import pytest

from fwgitops._poll import PollConfig
from fwgitops.push import (
    JobState,
    PushFailed,
    PushStatus,
    PushTimeout,
    UnexpectedStagedChanges,
    push_folder,
)

NOSLEEP = lambda _s: None  # noqa: E731
FAST = PollConfig(max_attempts=5, backoff_seconds=0)

#: Our automation identity — the only editor allowed to have touched the candidate.
US = "GitOps@1198884949.iam.panserviceaccount.com"


class FakeClient:
    def __init__(self, editors=None, statuses=None, job_id="job-1"):
        # Who edited the pending candidate. Default: only us.
        self.editors = list([US] if editors is None else editors)
        self.statuses = list(statuses or [JobState(PushStatus.SUCCESS)])
        self.job_id = job_id
        self.pushed_folders: list = []
        self.status_calls = 0

    def staged_editors(self, folder):
        return list(self.editors)

    def push(self, folder):
        self.pushed_folders.append(folder)
        return self.job_id

    def job_status(self, job_id):
        self.status_calls += 1
        return self.statuses[min(self.status_calls - 1, len(self.statuses) - 1)]


def run(client, **kw):
    kw.setdefault("allowed_editors", [US])
    return push_folder(client, "GitOps", poll=FAST, sleep=NOSLEEP, **kw)


# ── Happy path ────────────────────────────────────────────────────────────
def test_push_success():
    c = FakeClient()
    r = run(c)
    assert r.status == "success"
    assert r.job_id == "job-1"
    assert c.pushed_folders == ["GitOps"]
    assert r.editors == (US,)


def test_waits_for_job_to_finish():
    c = FakeClient(statuses=[
        JobState(PushStatus.PENDING),
        JobState(PushStatus.RUNNING),
        JobState(PushStatus.SUCCESS),
    ])
    assert run(c).status == "success"
    assert c.status_calls == 3


def test_result_is_evidence_shaped():
    ev = run(FakeClient()).to_evidence()
    assert set(ev) == {"folder", "status", "job_id", "editors"}
    assert ev["folder"] == "GitOps" and ev["status"] == "success"


# ── Fail closed (the security-critical guard) ─────────────────────────────
def test_refuses_to_push_when_a_human_edited_the_candidate():
    c = FakeClient(editors=[US, "human@corp"])   # out-of-band GUI edit
    with pytest.raises(UnexpectedStagedChanges) as ei:
        run(c)
    assert "human@corp" in str(ei.value)
    assert c.pushed_folders == []                # critically: never pushed


def test_unexpected_lists_only_outside_editors():
    c = FakeClient(editors=[US, "alice@corp", "bob@corp"])
    with pytest.raises(UnexpectedStagedChanges) as ei:
        run(c)
    assert ei.value.unexpected == ("alice@corp", "bob@corp")   # US excluded


def test_break_glass_override_allows_push():
    # Explicit human-approved override — never the default path.
    c = FakeClient(editors=[US, "human@corp"])
    r = run(c, allow_unexpected=True)
    assert r.status == "success"
    assert c.pushed_folders == ["GitOps"]


# ── Edge cases ────────────────────────────────────────────────────────────
def test_nothing_staged_is_a_noop_not_an_error():
    c = FakeClient(editors=[])
    r = run(c)
    assert r.status == "noop"
    assert r.job_id is None
    assert c.pushed_folders == []
    assert r.editors == ()


def test_multiple_allowed_editors_ok():
    # More than one automation identity is fine if all are allowed.
    c = FakeClient(editors=[US, "ci@corp"])
    r = run(c, allowed_editors=[US, "ci@corp"])
    assert r.status == "success"


# ── Failure paths ─────────────────────────────────────────────────────────
def test_job_failure_raises():
    c = FakeClient(statuses=[JobState(PushStatus.FAILED, "commit rejected")])
    with pytest.raises(PushFailed, match="commit rejected"):
        run(c)


def test_job_timeout_raises():
    c = FakeClient(statuses=[JobState(PushStatus.RUNNING)])  # never terminal
    with pytest.raises(PushTimeout, match="did not finish after 5"):
        run(c)
