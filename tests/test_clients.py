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

from fwgitops.clients import ScmDeviceClient, ScmProvisionClient, ScmPushClient
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
@pytest.mark.parametrize("payload", [{"job_id": "j1"}, {"jobId": "j1"}, {"id": "j1"}])
def test_push_returns_job_id(payload):
    assert ScmPushClient(session_for(payload)).push("GitOps") == "j1"


def test_push_without_job_id_raises():
    with pytest.raises(ScmApiError):
        ScmPushClient(session_for({"nothing": True})).push("GitOps")


def test_push_admin_scope_in_body():
    # Admin-scoped partial push: `admin` lists the identities whose staged
    # changes to commit, so out-of-band edits are never swept in.
    bodies = []

    def transport(method, url, headers, body):
        bodies.append(body)
        payload = {"access_token": "t"} if "oauth2" in url else {"job_id": "j1"}
        return 200, json.dumps(payload).encode()

    ScmPushClient(ScmSession(GOOD, transport=transport)).push(
        "prod-edge", admins=["GitOps@1198884949.iam.panserviceaccount.com"]
    )
    sent = json.loads(bodies[-1])
    assert sent["admin"] == ["GitOps@1198884949.iam.panserviceaccount.com"]


def test_push_without_admins_omits_scope():
    # Unscoped push (break-glass) sends no `admin` field -> whole candidate.
    bodies = []

    def transport(method, url, headers, body):
        bodies.append(body)
        payload = {"access_token": "t"} if "oauth2" in url else {"job_id": "j1"}
        return 200, json.dumps(payload).encode()

    ScmPushClient(ScmSession(GOOD, transport=transport)).push("prod-edge", admins=None)
    assert "admin" not in json.loads(bodies[-1])


def test_push_nothing_to_push_returns_none():
    # SCM reports no pending changes for the scope -> a no-op, not a failure.
    s = session_for({"message": "no changes to push"}, statuses={0: 400})
    assert ScmPushClient(s).push("GitOps", admins=["svc@iam"]) is None


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


# ── ScmDeviceClient (onboarding finalization + teardown) ──────────────────
def test_device_folder_finds_serial_entry():
    # A managed device appears in /folders as {name: <serial>, parent: <folder>}.
    payload = {"data": [
        {"name": "GitOps", "parent": "ngfw-shared"},
        {"name": "007955000891682", "parent": "prod-edge"},
    ]}
    assert ScmDeviceClient(session_for(payload)).device_folder("007955000891682") == "prod-edge"


def test_device_folder_none_when_not_placed():
    payload = {"data": [{"name": "prod-edge", "parent": "ngfw-shared"}]}
    assert ScmDeviceClient(session_for(payload)).device_folder("007955000891682") is None


def test_set_display_name_puts_device_body():
    bodies = []

    def transport(method, url, headers, body):
        bodies.append((method, url, body))
        payload = {"access_token": "t"} if "oauth2" in url else {"display_name": "n"}
        return 200, json.dumps(payload).encode()

    ScmDeviceClient(ScmSession(GOOD, transport=transport)).set_display_name(
        "007955000891682", "fw-prod-edge-682"
    )
    method, url, body = bodies[-1]
    assert method == "PUT" and url.endswith("/config/setup/v1/devices/007955000891682")
    assert json.loads(body) == {"display_name": "fw-prod-edge-682"}


def test_deregister_deletes_device():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        return 200, json.dumps({"access_token": "t"} if "oauth2" in url else {}).encode()

    ScmDeviceClient(ScmSession(GOOD, transport=transport)).deregister("007955000891682")
    assert calls[-1][0] == "DELETE"
    assert calls[-1][1].endswith("/config/setup/v1/devices/007955000891682")


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


# ── a timed-out GET is retried; a write never is ──────────────────────────
def _session(responses, calls):
    """Session whose transport replays `responses`, recording each call."""
    import socket

    from fwgitops.scmapi import ScmCredentials, ScmSession

    def transport(method, url, headers, body):
        # (method, url) — the OAuth token fetch is ALSO a POST, so counting
        # methods alone conflates it with the API call under test.
        calls.append((method, url))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return ScmSession(
        ScmCredentials(client_id="i", client_secret="s", scope="tsg_id:1"),
        transport=transport,
        sleep=lambda _s: None,
    )


def test_a_timed_out_GET_is_retried():
    """SCM's config-versions endpoints time out intermittently under load —
    observed 2026-08-06 during a burst of pushes, and again on 2026-08-08 when it
    failed the scheduled drift job outright. That is a transport hiccup, not
    drift, and a scheduled check that fails on the API being slow is one people
    stop reading."""
    import socket

    calls = []
    s = _session([(200, b'{"access_token":"t","expires_in":3600}'),
                  socket.timeout("read timed out"),
                  (200, b'{"data":[]}')], calls)
    assert s.request("GET", "/config/operations/v1/config-versions/running") == {"data": []}
    api = [m for m, u in calls if "config-versions" in u]
    assert api == ["GET", "GET"], "the GET should have been retried once"


def test_retries_are_EXHAUSTED_not_infinite_and_still_fail():
    """The point is to survive a hiccup, not to convert an unreachable API into a
    pass. `device-sync` must still exit non-zero when SCM cannot be read."""
    import socket

    import pytest as _pytest

    calls = []
    s = _session([(200, b'{"access_token":"t","expires_in":3600}')]
                 + [socket.timeout("read timed out")] * 5, calls)
    with _pytest.raises((socket.timeout, TimeoutError)):
        s.request("GET", "/config/operations/v1/config-versions/running")
    api = [m for m, u in calls if "config-versions" in u]
    assert len(api) == 3, "READ_RETRIES total attempts, then fail"


def test_a_timed_out_WRITE_is_NEVER_retried():
    """Retrying a POST could create a second object after the first quietly
    succeeded; retrying a DELETE could destroy something recreated in between.
    A write that times out is ambiguous, and guessing is worse than failing."""
    import socket

    import pytest as _pytest

    calls = []
    s = _session([(200, b'{"access_token":"t","expires_in":3600}'),
                  socket.timeout("read timed out"),
                  (200, b'{"id":"should-never-be-reached"}')], calls)
    with _pytest.raises((socket.timeout, TimeoutError)):
        s.request("POST", "/config/network/v1/zones", body={"name": "z"})
    api = [m for m, u in calls if "/config/network/v1/zones" in u]
    assert api == ["POST"], "a write must be attempted exactly once"
