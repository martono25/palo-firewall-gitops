"""Tests for the SCM auth/session layer.

This layer is VERIFIED behavior (it mirrors the exchange the spike exercised), so
it gets real tests. The endpoint mapping in fwgitops.clients is unverified and is
tested only for parsing tolerance, not for endpoint correctness.
"""

from __future__ import annotations

import json

import pytest

from fwgitops.scmapi import (
    ScmApiError,
    ScmAuthError,
    ScmConfigError,
    ScmCredentials,
    ScmSession,
)

GOOD = dict(
    client_id="GitOps@1198884949.iam.panserviceaccount.com",
    client_secret="s3cret",
    scope="tsg_id:1198884949",
)


class FakeTransport:
    """Scriptable transport: list of (status, payload) returned in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        status, payload = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return status, json.dumps(payload).encode()


def token_ok(expires_in=3600, tok="tok-1"):
    return (200, {"access_token": tok, "expires_in": expires_in})


# ── Credential validation (spike findings as fail-fast guards) ────────────
def test_valid_credentials_pass():
    ScmCredentials(**GOOD).validate()


def test_scope_without_tsg_prefix_rejected():
    with pytest.raises(ScmConfigError, match="tsg_id:"):
        ScmCredentials(**{**GOOD, "scope": "1198884949"}).validate()


def test_client_id_short_form_rejected():
    with pytest.raises(ScmConfigError, match="service account"):
        ScmCredentials(**{**GOOD, "client_id": "GitOps"}).validate()


def test_missing_fields_rejected():
    with pytest.raises(ScmConfigError, match="required"):
        ScmCredentials(**{**GOOD, "client_secret": ""}).validate()


def test_from_env_reads_and_validates():
    creds = ScmCredentials.from_env({
        "SCM_CLIENT_ID": GOOD["client_id"],
        "SCM_CLIENT_SECRET": GOOD["client_secret"],
        "SCM_SCOPE": GOOD["scope"],
    })
    assert creds.scope == "tsg_id:1198884949"


def test_from_env_rejects_bad_scope():
    with pytest.raises(ScmConfigError):
        ScmCredentials.from_env({
            "SCM_CLIENT_ID": GOOD["client_id"],
            "SCM_CLIENT_SECRET": "x",
            "SCM_SCOPE": "1198884949",   # the exact mistake from the spike
        })


# ── Token acquisition + caching ───────────────────────────────────────────
def test_token_uses_basic_auth_and_client_credentials():
    t = FakeTransport(token_ok())
    s = ScmSession(ScmCredentials(**GOOD), transport=t)
    assert s.token() == "tok-1"
    call = t.calls[0]
    assert call["headers"]["Authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in call["body"]
    assert b"tsg_id" in call["body"]


def test_token_is_cached():
    t = FakeTransport(token_ok())
    s = ScmSession(ScmCredentials(**GOOD), transport=t, now=lambda: 1000.0)
    s.token(); s.token(); s.token()
    assert len(t.calls) == 1  # only one auth round trip


def test_token_refreshes_after_expiry():
    clock = {"t": 1000.0}
    t = FakeTransport(token_ok(expires_in=100, tok="tok-1"), token_ok(expires_in=100, tok="tok-2"))
    s = ScmSession(ScmCredentials(**GOOD), transport=t, now=lambda: clock["t"])
    assert s.token() == "tok-1"
    clock["t"] += 200  # past expiry
    assert s.token() == "tok-2"
    assert len(t.calls) == 2


def test_refreshes_early_within_margin():
    clock = {"t": 0.0}
    t = FakeTransport(token_ok(expires_in=100, tok="a"), token_ok(expires_in=100, tok="b"))
    s = ScmSession(ScmCredentials(**GOOD), transport=t, now=lambda: clock["t"])
    assert s.token() == "a"
    clock["t"] = 50  # inside the 60s safety margin, so it should refresh early
    assert s.token() == "b"


# ── Auth errors carry actionable hints (the ones the spike cost us) ───────
@pytest.mark.parametrize("err,hint", [
    ("invalid_scope", "tsg_id"),
    ("invalid_client", "trailing newline"),
    ("unauthorized_client", "role assignment"),
])
def test_auth_errors_include_hint(err, hint):
    t = FakeTransport((400, {"error": err, "error_description": "nope"}))
    s = ScmSession(ScmCredentials(**GOOD), transport=t)
    with pytest.raises(ScmAuthError, match=hint):
        s.token()


# ── Authenticated requests ────────────────────────────────────────────────
def test_request_attaches_bearer_token():
    t = FakeTransport(token_ok(), (200, {"ok": True}))
    s = ScmSession(ScmCredentials(**GOOD), transport=t)
    assert s.request("GET", "/config/x") == {"ok": True}
    assert t.calls[1]["headers"]["Authorization"] == "Bearer tok-1"
    assert t.calls[1]["url"].startswith("https://api.sase.paloaltonetworks.com/config/x")


def test_request_raises_on_error_status():
    t = FakeTransport(token_ok(), (403, {"message": "forbidden"}))
    s = ScmSession(ScmCredentials(**GOOD), transport=t)
    with pytest.raises(ScmApiError) as ei:
        s.request("GET", "/config/x")
    assert ei.value.status == 403
