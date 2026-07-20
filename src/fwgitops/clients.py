"""SCM REST implementations of the PushClient and ProvisionClient protocols.

⚠️ ENDPOINT MAPPING IS UNVERIFIED. The auth/session layer (`fwgitops.scmapi`) is
verified — it is the exact exchange the spike exercised. The API *paths and
payload shapes* below are best-effort and marked `# VERIFY:`; confirm each
against the SCM API reference (or by watching the calls the Terraform provider
makes) before relying on them. This is the same honest posture the Terraform
module had before the spike, and the fix is the same: one place to correct.

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

    # VERIFY: all three paths below.
    PENDING_PATH = "/config/operations/v1/config-versions/candidate"
    PUSH_PATH = "/config/operations/v1/config-versions/push"
    JOB_PATH = "/config/operations/v1/jobs/{job_id}"

    def __init__(self, session: ScmSession):
        self.session = session

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

    def push(self, folder: str) -> str:
        """Start a folder-scoped push. Returns the job id."""
        # VERIFY: payload shape — SCM's push target is the FOLDER (spike finding #10).
        payload = self.session.request("POST", self.PUSH_PATH, body={"folders": [folder]})
        job_id = _first_present(payload, "job_id", "jobId", "id")
        if not job_id:
            raise ScmApiError(200, f"push response contained no job id: {payload}")
        return str(job_id)

    def job_status(self, job_id: str) -> JobState:
        payload = self.session.request("GET", self.JOB_PATH.format(job_id=job_id))
        raw = str(_first_present(payload, "status", "state", "result", default="")).lower()
        message = str(_first_present(payload, "message", "details", "error", default=""))
        # VERIFY the vocabulary. Unknown -> RUNNING so we keep polling; we never
        # claim success we cannot prove.
        if raw in ("success", "succeeded", "completed", "finished", "ok"):
            return JobState(PushStatus.SUCCESS, message)
        if raw in ("failed", "failure", "error", "cancelled", "canceled"):
            return JobState(PushStatus.FAILED, message or f"job reported {raw!r}")
        if raw in ("pending", "queued", "submitted"):
            return JobState(PushStatus.PENDING, message)
        return JobState(PushStatus.RUNNING, message)


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

    # VERIFY: every path below.
    DEVICE_PATH = "/config/setup/v1/devices/{device_id}"
    DEVICES_PATH = "/config/setup/v1/devices"
    LICENSE_PATH = "/config/setup/v1/devices/{device_id}/licenses"
    SNIPPET_BIND_PATH = "/config/setup/v1/devices/{device_id}/snippets"

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
