"""SCM REST implementations of the PushClient and ProvisionClient protocols.

⚠️ ENDPOINT MAPPING IS PARTIALLY VALIDATED (provider-binary analysis, 2026-07-19).

  EVIDENCED in provider v1.0.11: auth URL exactly as used here; API hosts
  `api.sase.paloaltonetworks.com` (default) and `api.strata.paloaltonetworks.com`;
  base paths `/config/objects/v1`, `/config/security/v1`, `/config/setup/v1`,
  `/config/network/v1`, `/config/deployment/v1`; resource segments `/addresses`,
  `/services`, `/tags/{id}`, `/security-rules`, `/folders`, `/devices`,
  `/snippets`, `/labels`. ProvisionClient paths below are corrected to match.

  DISPROVEN: the earlier push-endpoint guesses. "config/operations",
  "config-versions", "candidate-config" and "jobs/" appear ZERO times in the
  provider. Push paths are genuinely unknown — see ScmPushClient.

  STILL UNVERIFIED: payload shapes, per-device sub-paths, and licensing.

The auth/session layer (`fwgitops.scmapi`) is fully verified — it is the exact
exchange the spike exercised. Everything still marked `# VERIFY:` / ⛔ below
should be confirmed against the SCM API reference (or by watching the SCM UI's
network calls) before being relied on. Same honest posture the Terraform module
had before its spike, and the same fix: one place to correct.

The orchestration that consumes these — `fwgitops.push.push_folder` and
`fwgitops.provision.provision` — is fully built and tested against fakes, so
only this thin glue is outstanding.

Response parsing is deliberately tolerant: `_first_present` accepts several
plausible key spellings so a single wrong guess doesn't take the whole client
down, and unknown statuses map to RUNNING (keep polling) rather than SUCCESS
(never claim success we cannot prove).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from fwgitops.provision import Stage
from fwgitops.push import JobState, PushStatus
from fwgitops.scmapi import ScmApiError, ScmSession


def _first_present(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return default


class ScmPushClient:
    """PushClient over the SCM REST API (T13).

    Consumed by `fwgitops.push.push_folder`, which owns the fail-closed guard,
    the folder-scoped push semantics, and the bounded job poll.
    """

    # ✅ CONFIRMED by live probe (2026-07-19, GET returned 200):
    PENDING_PATH = "/config/operations/v1/config-versions/candidate"
    JOB_PATH = "/config/operations/v1/jobs/{job_id}"
    # ⚠️ MOST LIKELY but not confirmed: GET is permissive on this route (any
    # trailing segment returns the candidate), so GET-probing cannot distinguish
    # the push verb. `candidate:push` is the AIP custom-method form and the best
    # candidate. CONFIRM from the SCM UI's network call on "Push Config" before
    # relying on it. Injectable below — no code edit needed once known.
    PUSH_PATH = "/config/operations/v1/config-versions/candidate:push"

    def __init__(
        self,
        session: ScmSession,
        *,
        pending_path: Optional[str] = None,
        push_path: Optional[str] = None,
        job_path: Optional[str] = None,
    ):
        self.session = session
        # Injectable so the discovered paths need no code change.
        self.PENDING_PATH = pending_path or self.PENDING_PATH
        self.PUSH_PATH = push_path or self.PUSH_PATH
        self.JOB_PATH = job_path or self.JOB_PATH

    def list_staged(self, folder: str) -> Iterable[str]:
        """Identifiers of changes currently staged (uncommitted) in `folder`.

        These identifiers must line up with what the compiler considers
        "expected" (object/rule names) or the fail-closed guard will misfire.
        VERIFY the response shape and align the identifier extraction.
        """
        payload = self.session.request("GET", self.PENDING_PATH, params={"folder": folder})
        items: List[Any] = _first_present(payload, "data", "items", "changes", default=[]) or []
        out: List[str] = []
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = _first_present(item, "name", "object_name", "id")
                if name:
                    out.append(str(name))
        return out

    #: Ordered candidates for the push route, best guess first. GET-probing
    #: cannot distinguish them (the route is permissive), so the first real POST
    #: is the test. A wrong path fails loudly with these listed — a 30-second fix.
    PUSH_PATH_CANDIDATES = (
        "/config/operations/v1/config-versions/candidate:push",
        "/config/operations/v1/config-versions/push",
        "/config/operations/v1/config-versions:push",
        "/config/operations/v1/push",
    )

    def push(self, folder: str) -> str:
        """Start a folder-scoped push. Returns the job id.

        SCM's push target is the FOLDER (spike finding #10). If PUSH_PATH is
        wrong the API answers 404/405 and we re-raise with the remaining
        candidates so the fix is obvious and one line.
        """
        try:
            payload = self.session.request("POST", self.PUSH_PATH, body={"folders": [folder]})
        except ScmApiError as e:
            if e.status in (404, 405):
                others = [c for c in self.PUSH_PATH_CANDIDATES if c != self.PUSH_PATH]
                raise ScmApiError(
                    e.status,
                    f"push path {self.PUSH_PATH!r} rejected ({e.status}). This path is the one "
                    f"part of the SCM API we could not confirm by probing. Try one of: "
                    f"{', '.join(others)} — pass it as ScmPushClient(session, push_path=...), "
                    f"no code change needed. Confirm definitively from the SCM UI's network "
                    f"call on 'Push Config'."
                ) from e
            raise
        job_id = _first_present(payload, "job_id", "jobId", "id")
        if not job_id:
            raise ScmApiError(200, f"push response contained no job id: {payload}")
        return str(job_id)

    def job_status(self, job_id: str) -> JobState:
        """Map SCM's PAN-OS-style job record to a JobState.

        ⚠️ SCM uses TWO fields (confirmed live): `status_str` says whether the job
        is DONE (PEND / ACT / FIN), `result_str` says whether it SUCCEEDED
        (OK / FAIL). A job can be FIN with result FAIL — treating "finished" as
        "succeeded" would silently report a failed push as success, so the
        result field is authoritative for the outcome.
        """
        payload = self.session.request("GET", self.JOB_PATH.format(job_id=job_id))
        body = payload.get("data", payload)
        if isinstance(body, list):
            body = body[0] if body else {}

        status = str(_first_present(body, "status_str", "job_status", "status", default="")).lower()
        result = str(_first_present(body, "result_str", "job_result", "result", default="")).lower()
        message = str(_first_present(body, "summary", "description", "message", default=""))
        pct = _first_present(body, "percent")
        if pct is not None and not message:
            message = f"{pct}%"

        if status in ("fin", "finished", "completed", "done"):
            if result in ("ok", "success", "succeeded"):
                return JobState(PushStatus.SUCCESS, message)
            # FIN but not OK (or result unknown) -> failed. Never assume success.
            shown = result or "unknown"
            return JobState(PushStatus.FAILED, message or f"job finished with result {shown!r}")
        if status in ("pend", "pending", "queued", "submitted"):
            return JobState(PushStatus.PENDING, message)
        # ACT / unknown -> keep polling rather than claim an outcome.
        return JobState(PushStatus.RUNNING, message)

    def candidate_editors(self, folder: str) -> List[str]:
        """Who edited the pending candidate config.

        The fail-closed guard's real signal: SCM tracks a candidate config
        VERSION (with `edited_by` / `admin`), not a list of changed object names,
        so we cannot diff object identifiers. Instead we assert the candidate was
        touched ONLY by our automation identity — if a human staged something in
        this folder, their identity appears here and we must refuse to push.
        """
        payload = self.session.request("GET", self.PENDING_PATH, params={"folder": folder})
        items = payload if isinstance(payload, list) else _first_present(payload, "data", default=[])
        editors: List[str] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            for key in ("edited_by", "admin"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    editors.append(val)
                elif isinstance(val, list):
                    editors.extend(str(v) for v in val if v)
        return sorted(set(editors))


class ScmProvisionClient:
    """ProvisionClient over the SCM REST API (Day-1, T3).

    ⚠️ The most speculative part of this module. Licensing in particular may not
    live in the SCM API at all — activation may run through the Customer Support
    Portal / deployment profiles instead, in which case `activate_license` should
    call that service (or be a no-op if licensing is handled at bootstrap).
    Confirm during the device-onboarding sub-spike.

    The orchestration (`fwgitops.provision.provision`) is re-entrant and derives
    the stage from reality, so a partially-correct client still fails safe: an
    unknown stage never advances past what it can prove.
    """

    # Base path and resource segments are EVIDENCED in provider v1.0.11
    # ("/config/setup/v1" + "/devices", "/folders", "/snippets" all present in
    # the binary). The per-device sub-paths and payload shapes are still VERIFY.
    DEVICE_PATH = "/config/setup/v1/devices/{device_id}"      # evidenced base+segment
    DEVICES_PATH = "/config/setup/v1/devices"                  # evidenced
    FOLDERS_PATH = "/config/setup/v1/folders"                  # evidenced (Day-1 owns folders)
    SNIPPETS_PATH = "/config/setup/v1/snippets"                # evidenced
    # VERIFY: sub-path shape for binding a snippet to a device.
    SNIPPET_BIND_PATH = "/config/setup/v1/devices/{device_id}/snippets"
    # ⛔ NO EVIDENCE: licensing is very likely NOT an SCM API operation at all —
    # activation runs through the CSP / deployment profiles. Confirm in the
    # device-onboarding sub-spike; this may become a no-op here.
    LICENSE_PATH = "/config/setup/v1/devices/{device_id}/licenses"

    def __init__(self, session: ScmSession):
        self.session = session

    def current_stage(self, device_id: str) -> Stage:
        """Derive the real stage from SCM. This is what makes re-entrancy work.

        Fails SAFE: anything we cannot positively confirm reads as an earlier
        stage, so the orchestrator re-does an idempotent step rather than
        skipping a step that never happened.
        """
        try:
            device = self.session.request("GET", self.DEVICE_PATH.format(device_id=device_id))
        except ScmApiError as e:
            if e.status == 404:
                return Stage.ABSENT
            raise
        if not device:
            return Stage.ABSENT

        # VERIFY: field names for licensing / folder binding / snippet binding / sync.
        licensed = bool(_first_present(device, "licensed", "license_active", default=False))
        folder = _first_present(device, "folder", "folder_name")
        baselined = bool(_first_present(device, "snippets", "config_bound", default=False))
        in_sync = str(_first_present(device, "sync_status", "status", default="")).lower() in (
            "in sync", "in_sync", "synced", "connected",
        )

        if not licensed:
            return Stage.INSTANTIATED
        if not folder:
            return Stage.LICENSED
        if not baselined:
            return Stage.ONBOARDED
        return Stage.READY if in_sync else Stage.BASELINED

    def activate_license(self, device_id: str) -> None:
        # VERIFY: licensing may be a CSP/deployment-profile operation, not SCM.
        # The orchestrator wraps this in a bounded retry (it is the flaky step).
        self.session.request("POST", self.LICENSE_PATH.format(device_id=device_id))

    def onboard(self, device_id: str, folder: str) -> None:
        # VERIFY: is folder assignment a PUT on the device, or a separate op?
        self.session.request(
            "PUT", self.DEVICE_PATH.format(device_id=device_id), body={"folder": folder}
        )

    def apply_baseline(self, device_id: str, snippet: str) -> None:
        # VERIFY: snippet binding shape.
        self.session.request(
            "POST", self.SNIPPET_BIND_PATH.format(device_id=device_id), body={"name": snippet}
        )

    def is_connected(self, device_id: str) -> bool:
        device = self.session.request("GET", self.DEVICE_PATH.format(device_id=device_id))
        status = str(_first_present(device, "connection_status", "status", default="")).lower()
        return status in ("connected", "in sync", "in_sync", "synced")
