"""Tests for the SCM REST clients.

Endpoint paths + push body key are CONFIRMED against the live tenant. These
tests cover parsing and the
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


# SCM uses the PAN-OS two-field model (confirmed live): status_str says whether
# the job is DONE, result_str says whether it SUCCEEDED. A FIN job with a FAIL
# result must NEVER read as success.
@pytest.mark.parametrize("payload,expected", [
    ({"status_str": "FIN", "result_str": "OK"}, PushStatus.SUCCESS),
    ({"status_str": "FIN", "result_str": "FAIL"}, PushStatus.FAILED),
    ({"status_str": "FIN"}, PushStatus.FAILED),            # finished, result unknown -> not success
    ({"status_str": "PEND"}, PushStatus.PENDING),
    ({"status_str": "ACT"}, PushStatus.RUNNING),
    ({"status_str": "wibble"}, PushStatus.RUNNING),        # unknown -> keep polling
])
def test_job_status_panos_two_field_model(payload, expected):
    assert ScmPushClient(session_for(payload)).job_status("j1").status is expected


def test_finished_but_failed_is_never_success():
    # The bug this guards: treating "finished" as "succeeded" would silently
    # report a failed push as a successful one.
    st = ScmPushClient(session_for({"status_str": "FIN", "result_str": "FAIL"})).job_status("j1")
    assert st.status is PushStatus.FAILED


def test_job_record_unwrapped_from_data_envelope():
    # SCM list responses use {data, limit, offset, total}.
    payload = {"data": [{"status_str": "FIN", "result_str": "OK"}], "total": 1}
    assert ScmPushClient(session_for(payload)).job_status("j1").status is PushStatus.SUCCESS


def test_candidate_editors_extracted_for_the_fail_closed_guard():
    # The real guard signal: who touched the pending candidate config.
    payload = [{"edited_by": "human@corp", "admin": "GitOps@1198884949.iam.panserviceaccount.com"}]
    editors = ScmPushClient(session_for(payload)).candidate_editors("GitOps")
    assert "human@corp" in editors


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


def test_push_body_uses_plural_folders_key():
    # From the LIVE tenant (outranks the SDK): the schema validator accepts
    # `folders` (plural) and rejects `folder` (API_I00035). The scm-go SDK's
    # `folder` has drifted from the deployed API. Key is injectable.
    bodies = []

    def transport(method, url, headers, body):
        bodies.append(body)
        payload = {"access_token": "t"} if "oauth2" in url else {"job_id": "j1"}
        return 200, json.dumps(payload).encode()

    ScmPushClient(ScmSession(GOOD, transport=transport)).push("GitOps")
    sent = json.loads(bodies[-1])
    assert sent["folders"] == ["GitOps"]     # plural key — live tenant schema
    assert "folder" not in sent
