"""SCM REST API session — auth, token caching, and request plumbing.

The auth flow here is VERIFIED: it is exactly the exchange exercised during the
spike (HTTP Basic with the service-account client id/secret, `client_credentials`
grant, `tsg_id:<TSG>` scope → a short-lived JWT). The provider does this
internally; we need it too for the operations Terraform cannot do (push, and the
Day-1 onboarding steps).

Spike findings encoded as fail-fast validation, so nobody loses an hour to them
again (we did):
  * scope MUST be `tsg_id:<TSG_ID>` — a bare TSG returns `invalid_scope`
  * client_id is the full `name@<tsg>.iam.panserviceaccount.com` form
  * a roleless / mis-scoped service account authenticates but then fails on real
    operations, so surface auth vs authorization distinctly

`transport` is injectable so the session is unit-testable without network.
Zero third-party dependencies (stdlib urllib); swap in requests/httpx later if
you'd rather — only `_urllib_transport` would change.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

DEFAULT_AUTH_URL = "https://auth.apps.paloaltonetworks.com/auth/v1/oauth2/access_token"
DEFAULT_HOST = "api.sase.paloaltonetworks.com"

#: Refresh a little before actual expiry so a long apply never races the clock.
EXPIRY_MARGIN_SECONDS = 60

_SCOPE_RE = re.compile(r"^tsg_id:\S+$")
_CLIENT_ID_RE = re.compile(r"^[^@\s]+@\S+\.iam\.panserviceaccount\.com$")

#: (method, url, headers, body) -> (status_code, body_bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


class ScmAuthError(Exception):
    """Authentication failed (bad credentials, scope, or grant)."""


class ScmApiError(Exception):
    """An SCM API call failed."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"SCM API error {status}: {message}")


class ScmConfigError(Exception):
    """Credentials are missing or malformed — raised before any network call."""


@dataclass
class ScmCredentials:
    client_id: str
    #: repr=False so a traceback, debug print or logged locals cannot render the
    #: live tenant secret in cleartext.
    client_secret: str = field(repr=False)
    scope: str
    auth_url: str = DEFAULT_AUTH_URL
    host: str = DEFAULT_HOST

    def validate(self) -> "ScmCredentials":
        if not self.client_id or not self.client_secret or not self.scope:
            raise ScmConfigError(
                "client_id, client_secret and scope are all required "
                "(set SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE)"
            )
        if not _SCOPE_RE.match(self.scope):
            raise ScmConfigError(
                f"scope {self.scope!r} must be of the form 'tsg_id:<TSG_ID>' — a bare TSG id "
                "is rejected by the auth server with 'invalid_scope'"
            )
        if not _CLIENT_ID_RE.match(self.client_id):
            raise ScmConfigError(
                f"client_id {self.client_id!r} does not look like a service account; expected "
                "'<name>@<tsg_id>.iam.panserviceaccount.com'"
            )
        return self

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "ScmCredentials":
        import os

        e = env if env is not None else dict(os.environ)
        return cls(
            client_id=e.get("SCM_CLIENT_ID", ""),
            client_secret=e.get("SCM_CLIENT_SECRET", ""),
            scope=e.get("SCM_SCOPE", ""),
            auth_url=e.get("SCM_AUTH_URL", DEFAULT_AUTH_URL),
            host=e.get("SCM_HOST", DEFAULT_HOST),
        ).validate()


def _urllib_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes]
) -> Tuple[int, bytes]:  # pragma: no cover - exercised only against a live API
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class ScmSession:
    """Authenticated SCM API session with a cached bearer token."""

    credentials: ScmCredentials = field(repr=False)  # carries client_secret
    transport: Transport = _urllib_transport
    now: Callable[[], float] = time.time
    _token: Optional[str] = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    # ── auth ──────────────────────────────────────────────────────────────
    def token(self) -> str:
        """Return a valid bearer token, refreshing shortly before expiry."""
        if self._token and self.now() < self._expires_at - EXPIRY_MARGIN_SECONDS:
            return self._token

        creds = self.credentials
        basic = base64.b64encode(
            f"{creds.client_id}:{creds.client_secret}".encode("utf-8")
        ).decode("ascii")
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": creds.scope}
        ).encode("utf-8")
        status, raw = self.transport(
            "POST",
            creds.auth_url,
            {
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body,
        )
        payload = _json(raw)
        if status != 200 or "access_token" not in payload:
            err = payload.get("error", "unknown_error")
            desc = payload.get("error_description", "")
            hint = ""
            if err == "invalid_scope":
                hint = " (scope must be 'tsg_id:<TSG_ID>')"
            elif err == "invalid_client":
                hint = " (check client_id/secret; a trailing newline in the secret is a common cause)"
            elif err in ("unauthorized_client", "access_denied"):
                hint = " (the account authenticated but is not authorized — check its role assignment)"
            raise ScmAuthError(f"{err}: {desc}{hint}")

        self._token = payload["access_token"]
        self._expires_at = self.now() + float(payload.get("expires_in", 3600))
        return self._token

    # ── requests ──────────────────────────────────────────────────────────
    def request(
        self, method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None
    ) -> dict:
        """Make an authenticated call. `path` is relative to the API host."""
        url = f"https://{self.credentials.host}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        raw_body = json.dumps(body).encode("utf-8") if body is not None else None
        status, raw = self.transport(
            method,
            url,
            {
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            raw_body,
        )
        payload = _json(raw)
        if status >= 400:
            raise ScmApiError(status, json.dumps(payload))
        return payload


def _json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw[:500].decode("utf-8", "replace")}
    return parsed if isinstance(parsed, dict) else {"data": parsed}
