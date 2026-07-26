"""Tests for the SCM push step (T13) — the atomic commit boundary.

Safe by construction: the push is ADMIN-SCOPED, committing only our service
account's staged changes, so a shared-candidate folder with out-of-band edits is
never swept in. (The earlier detect-drift guard read committed version HISTORY,
which could never signal current pending drift — removed.)
"""

from __future__ import annotations

import pytest

from fwgitops._poll import PollConfig
from fwgitops.push import JobState, PushFailed, PushStatus, PushTimeout, push_folder

NOSLEEP = lambda _s: None  # noqa: E731
FAST = PollConfig(max_attempts=5, backoff_seconds=0)

#: Our automation identity — the service account the commit is scoped to.
US = "GitOps@1198884949.iam.panserviceaccount.com"


class FakeClient:
    def __init__(self, statuses=None, job_id="job-1", nothing_to_push=False):
        self.statuses = list(statuses or [JobState(PushStatus.SUCCESS)])
        self.job_id = job_id
        self.nothing_to_push = nothing_to_push
        self.pushes: list = []  # (folder, admins) recorded per push call
        self.status_calls = 0

    def push(self, folder, *, admins):
        self.pushes.append((folder, None if admins is None else list(admins)))
        return None if self.nothing_to_push else self.job_id

    def job_status(self, job_id):
        self.status_calls += 1
        return self.statuses[min(self.status_calls - 1, len(self.statuses) - 1)]


def run(client, **kw):
    kw.setdefault("admins", [US])
    return push_folder(client, "GitOps", poll=FAST, sleep=NOSLEEP, **kw)


# ── Happy path ────────────────────────────────────────────────────────────
def test_push_success_is_scoped_to_our_admin():
    c = FakeClient()
    r = run(c)
    assert r.status == "success"
    assert r.job_id == "job-1"
    assert c.pushes == [("GitOps", [US])]  # commit scoped to our service account
    assert r.admins == (US,)


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
    assert set(ev) == {"folder", "status", "job_id", "admins"}
    assert ev["folder"] == "GitOps" and ev["status"] == "success"


# ── Safe by construction: admin scoping ───────────────────────────────────
def test_multiple_admins_scope_passed_through():
    c = FakeClient()
    run(c, admins=[US, "ci@corp"])
    assert c.pushes == [("GitOps", [US, "ci@corp"])]


def test_all_admins_pushes_unscoped_break_glass():
    # Break-glass: commit the WHOLE candidate (baseline absorption), no admin scope.
    c = FakeClient()
    r = run(c, all_admins=True)
    assert c.pushes == [("GitOps", None)]  # unscoped -> whole candidate
    assert r.admins == ()


# ── Edge cases ────────────────────────────────────────────────────────────
def test_nothing_to_push_is_a_noop_not_an_error():
    c = FakeClient(nothing_to_push=True)
    r = run(c)
    assert r.status == "noop"
    assert r.job_id is None
    assert c.pushes == [("GitOps", [US])]  # attempted, SCM reported nothing staged


# ── Failure paths ─────────────────────────────────────────────────────────
def test_job_failure_raises():
    c = FakeClient(statuses=[JobState(PushStatus.FAILED, "commit rejected")])
    with pytest.raises(PushFailed, match="commit rejected"):
        run(c)


def test_job_timeout_raises():
    c = FakeClient(statuses=[JobState(PushStatus.RUNNING)])  # never terminal
    with pytest.raises(PushTimeout, match="did not finish after 5"):
        run(c)
