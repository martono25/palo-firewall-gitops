"""Tests for the SCM REST clients.

⚠️ These do NOT verify endpoint correctness — the paths are unverified guesses.
What they DO verify is the parsing tolerance and, most importantly, the
fail-safe defaults: an unknown job status must keep polling rather than claim
success, and an unknown device state must read as an EARLIER stage so the
re-entrant orchestrator redoes an idempotent step instead of skipping one.
"""

from __future__ import annotations

import json

import pytest

from fwgitops.clients import ScmProvisionClient, ScmPushClient
from fwgitops.provision import Stage
from fwgitops.push import PushStatus
from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession

GOOD = ScmCredentials(
    client_id="GitOps@1198884949.iam.panserviceaccount.com",
    client_secret="s3cret",
    scope="tsg_id:1198884949",
)


def session_for(*payloads, statuses=None):
    """A session whose first call returns a token, then the given payloads."""
    responses = [(200, {"access_token": "t", "expires_in": 3600})]
    for i, p in enumerate(payloads):
        responses.append(((statuses or {}).get(i, 200), p))

    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        status, payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return status, json.dumps(payload).encode()

    s = ScmSession(GOOD, transport=transport)
    s.calls = calls  # type: ignore[attr-defined]
    return s


# ── ScmPushClient ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [
    {"data": [{"name": "addr-abc"}, {"name": "svc-def"}]},
    {"items": ["addr-abc", "svc-def"]},
    {"changes": [{"object_name": "addr-abc"}, {"id": "svc-def"}]},
])
def test_list_staged_tolerates_shapes(payload):
    c = ScmPushClient(session_for(payload))
    assert sorted(c.list_staged("GitOps")) == ["addr-abc", "svc-def"]


def test_list_staged_empty():
    assert list(ScmPushClient(session_for({})).list_staged("GitOps")) == []


@pytest.mark.parametrize("payload", [{"job_id": "j1"}, {"jobId": "j1"}, {"id": "j1"}])
def test_push_returns_job_id(payload):
    assert ScmPushClient(session_for(payload)).push("GitOps") == "j1"


def test_push_without_job_id_raises():
    with pytest.raises(ScmApiError):
        ScmPushClient(session_for({"nothing": True})).push("GitOps")


@pytest.mark.parametrize("raw,expected", [
    ("success", PushStatus.SUCCESS),
    ("completed", PushStatus.SUCCESS),
    ("failed", PushStatus.FAILED),
    ("error", PushStatus.FAILED),
    ("pending", PushStatus.PENDING),
    ("running", PushStatus.RUNNING),
])
def test_job_status_vocabulary(raw, expected):
    assert ScmPushClient(session_for({"status": raw})).job_status("j1").status is expected


def test_unknown_job_status_keeps_polling_not_success():
    # Fail-safe: never claim a success we cannot prove.
    assert ScmPushClient(session_for({"status": "wibble"})).job_status("j1").status is PushStatus.RUNNING


# ── ScmProvisionClient ────────────────────────────────────────────────────
def test_missing_device_is_absent():
    c = ScmProvisionClient(session_for({"message": "not found"}, statuses={0: 404}))
    assert c.current_stage("vm-1") is Stage.ABSENT


@pytest.mark.parametrize("device,expected", [
    ({"licensed": False}, Stage.INSTANTIATED),
    ({"licensed": True}, Stage.LICENSED),
    ({"licensed": True, "folder": "GitOps"}, Stage.ONBOARDED),
    ({"licensed": True, "folder": "GitOps", "snippets": ["base"]}, Stage.BASELINED),
    ({"licensed": True, "folder": "GitOps", "snippets": ["base"], "sync_status": "in sync"}, Stage.READY),
])
def test_stage_derivation(device, expected):
    assert ScmProvisionClient(session_for(device)).current_stage("vm-1") is expected


def test_unknown_device_shape_reads_as_earliest_stage():
    # Fail-safe: an unrecognised payload must NOT look further along than it is,
    # so the re-entrant orchestrator redoes an idempotent step rather than
    # skipping one that never happened.
    assert ScmProvisionClient(session_for({"weird": 1})).current_stage("vm-1") is Stage.INSTANTIATED


@pytest.mark.parametrize("status,expected", [
    ("connected", True), ("in sync", True), ("disconnected", False), ("", False),
])
def test_is_connected(status, expected):
    c = ScmProvisionClient(session_for({"connection_status": status}))
    assert c.is_connected("vm-1") is expected
