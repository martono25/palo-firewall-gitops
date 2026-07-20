"""Tests for the SCM push step (T13) — the atomic commit boundary.

The fail-closed guard is the security-critical behavior here: a folder-scoped
push commits everything staged, so pushing with someone else's unreviewed change
present is the failure mode we must never allow.
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

OURS = ["addr-abc", "svc-def", "REQ-2026-0417"]


class FakeClient:
    def __init__(self, staged=None, statuses=None, job_id="job-1"):
        self.staged = list(OURS if staged is None else staged)
        # Sequence of JobState returned by successive job_status() calls.
        self.statuses = list(statuses or [JobState(PushStatus.SUCCESS)])
        self.job_id = job_id
        self.pushed_folders: list = []
        self.status_calls = 0

    def list_staged(self, folder):
        return list(self.staged)

    def push(self, folder):
        self.pushed_folders.append(folder)
        return self.job_id

    def job_status(self, job_id):
        self.status_calls += 1
        idx = min(self.status_calls - 1, len(self.statuses) - 1)
        return self.statuses[idx]


def run(client, **kw):
    kw.setdefault("expected", OURS)
    return push_folder(client, "GitOps", poll=FAST, sleep=NOSLEEP, **kw)


# ── Happy path ────────────────────────────────────────────────────────────
def test_push_success():
    c = FakeClient()
    r = run(c)
    assert r.status == "success"
    assert r.job_id == "job-1"
    assert c.pushed_folders == ["GitOps"]
    assert r.pushed == tuple(sorted(OURS))
    assert r.missing == ()


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
    assert set(ev) == {"folder", "status", "job_id", "pushed", "missing"}
    assert ev["folder"] == "GitOps" and ev["status"] == "success"


# ── Fail closed (the security-critical guard) ─────────────────────────────
def test_refuses_to_push_unexpected_staged_change():
    # Someone made an out-of-band GUI edit in the same folder.
    c = FakeClient(staged=OURS + ["rogue-any-any-rule"])
    with pytest.raises(UnexpectedStagedChanges) as ei:
        run(c)
    assert "rogue-any-any-rule" in str(ei.value)
    assert c.pushed_folders == []  # critically: never pushed


def test_unexpected_lists_only_the_delta():
    c = FakeClient(staged=OURS + ["rogue-1", "rogue-2"])
    with pytest.raises(UnexpectedStagedChanges) as ei:
        run(c)
    assert ei.value.unexpected == ("rogue-1", "rogue-2")


def test_break_glass_override_allows_push():
    # Explicit human-approved override — never the default path.
    c = FakeClient(staged=OURS + ["rogue-1"])
    r = run(c, allow_unexpected=True)
    assert r.status == "success"
    assert c.pushed_folders == ["GitOps"]


# ── Edge cases ────────────────────────────────────────────────────────────
def test_nothing_staged_is_a_noop_not_an_error():
    c = FakeClient(staged=[])
    r = run(c)
    assert r.status == "noop"
    assert r.job_id is None
    assert c.pushed_folders == []          # nothing to push
    assert r.missing == tuple(sorted(OURS))


def test_partial_stage_records_missing_but_still_pushes():
    c = FakeClient(staged=["addr-abc"])
    r = run(c)
    assert r.status == "success"
    assert r.pushed == ("addr-abc",)
    assert set(r.missing) == {"svc-def", "REQ-2026-0417"}


# ── Failure paths ─────────────────────────────────────────────────────────
def test_job_failure_raises():
    c = FakeClient(statuses=[JobState(PushStatus.FAILED, "commit rejected")])
    with pytest.raises(PushFailed, match="commit rejected"):
        run(c)


def test_job_timeout_raises():
    c = FakeClient(statuses=[JobState(PushStatus.RUNNING)])  # never terminal
    with pytest.raises(PushTimeout, match="did not finish after 5"):
        run(c)
