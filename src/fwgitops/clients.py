"""SCM REST implementations of the PushClient and ProvisionClient protocols.

SCM PUSH PATH (T13) IS FULLY CONFIRMED (2026-07-19):
  * auth/session layer verified live (fwgitops.scmapi)
  * PENDING + JOB paths confirmed by live probe (GET 200)
  * PUSH path confirmed by live POST (400 = path+verb correct, body wrong) and by
    the SDK method PushCandidateConfigVersions
  * PUSH BODY confirmed from the SDK struct PushCandidateConfigVersionsRequest:
    field is `folder` (singular key, array value), plus optional devices/admin/
    description. `folders` (plural) is rejected — see ScmPushClient.push.
  * JOB status uses the PAN-OS two-field model: status_str (done?) + result_str
    (ok?). Sources: pkg.go.dev/github.com/paloaltonetworks/scm-go, live tenant.

ScmProvisionClient paths (Day-1) are EVIDENCED from provider-binary analysis
(base `/config/setup/v1` + segments `/devices`, `/folders`, `/snippets` all
present in v1.0.11) but the per-device sub-paths, payload shapes, and licensing
remain `# VERIFY:` — the device-onboarding sub-spike (needs a VM-Series) closes
those, same honest posture the Terraform module had before its spike.

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


#: Phrases SCM uses when a scoped push has no pending changes to commit. Treated
#: as a no-op (not a failure) so a steady-state pipeline run on an unchanged
#: folder is green. VERIFY (live, VM up): confirm SCM's exact no-changes signal
#: (status + message) and tighten this to match rather than a broad phrase match.
_NOTHING_TO_PUSH = ("no changes", "nothing to push", "no candidate", "no pending", "up to date")


def _is_nothing_to_push(err: "ScmApiError") -> bool:
    text = str(err).lower()
    return any(p in text for p in _NOTHING_TO_PUSH)


class ScmPushClient:
    """PushClient over the SCM REST API (T13).

    Consumed by `fwgitops.push.push_folder`, which owns the fail-closed guard,
    the folder-scoped push semantics, and the bounded job poll.
    """

    # ✅ ALL CONFIRMED (2026-07-19):
    #   PENDING/JOB by live probe (GET 200); PUSH path by live POST (400 = path
    #   + verb correct, body wrong) and by the SDK method PushCandidateConfigVersions.
    PENDING_PATH = "/config/operations/v1/config-versions/candidate"
    JOB_PATH = "/config/operations/v1/jobs/{job_id}"
    PUSH_PATH = "/config/operations/v1/config-versions/candidate:push"
    #: Live tenant accepts `folders` (plural); the scm-go SDK says `folder`. Live
    #: wins. Injectable so an API-version flip needs no code change.
    PUSH_FOLDER_KEY = "folders"

    def __init__(
        self,
        session: ScmSession,
        *,
        pending_path: Optional[str] = None,
        push_path: Optional[str] = None,
        job_path: Optional[str] = None,
        push_folder_key: Optional[str] = None,
    ):
        self.session = session
        # Injectable so the discovered paths need no code change.
        self.PENDING_PATH = pending_path or self.PENDING_PATH
        self.PUSH_PATH = push_path or self.PUSH_PATH
        self.JOB_PATH = job_path or self.JOB_PATH
        self.PUSH_FOLDER_KEY = push_folder_key or self.PUSH_FOLDER_KEY

    def push(
        self, folder: str, *, admins: Optional[List[str]] = None, description: str = "fwgitops"
    ) -> Optional[str]:
        """Start a folder-scoped push. Returns the job id, or None if nothing to push.

        ADMIN-SCOPED PARTIAL PUSH is how we fail safe. SCM has a SHARED candidate:
        a push commits EVERY editor's pending changes in the folder, not just ours
        (confirmed live — a single version bundled our service account + a human).
        The push request's `admin` field (OpenAPI `PushCandidateConfigVersions_request`:
        "List the administrators and/or service accounts in this field") scopes the
        commit to just those identities' staged changes. So we pass our service
        account and NEVER sweep in out-of-band edits — safe by construction, not by
        detecting drift after the fact (the old `staged_editors` guard read committed
        version HISTORY, which can never signal current pending drift).

        `admins=None` means unscoped — commit the WHOLE candidate (break-glass /
        baseline absorption). Body key for folders is `folders` (plural) — the LIVE
        API outranks the SDK (which says `folder`); `PUSH_FOLDER_KEY` is injectable.
        """
        body: Dict[str, Any] = {self.PUSH_FOLDER_KEY: [folder], "description": description}
        if admins:
            body["admin"] = list(admins)
        try:
            payload = self.session.request("POST", self.PUSH_PATH, body=body)
        except ScmApiError as e:
            if _is_nothing_to_push(e):
                return None
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


class ScmDeviceClient:
    """DeviceClient over the SCM REST API (onboarding finalization + teardown).

    All three operations are CONFIRMED against the live tenant and readable/
    writable by the POLICY service account (2026-07-26):
      * placement:   GET /config/setup/v1/folders -> a managed device appears as
        an entry {name: <serial>, parent: <folder>}.
      * display name: PUT /config/setup/v1/devices/{serial} {"display_name": ...}
        (verified: 007955000891682 PA-VM -> fw-prod-edge-682).
      * deregister:   DELETE /config/setup/v1/devices/{serial}.
    """

    FOLDERS_PATH = "/config/setup/v1/folders"
    DEVICE_PATH = "/config/setup/v1/devices/{serial}"

    def __init__(self, session: ScmSession):
        self.session = session

    def device_folder(self, serial: str) -> Optional[str]:
        """The folder a managed device sits in, or None if not yet placed."""
        payload = self.session.request("GET", self.FOLDERS_PATH)
        items = payload if isinstance(payload, list) else _first_present(payload, "data", default=[])
        for f in items or []:
            if isinstance(f, dict) and str(f.get("name")) == serial:
                folder = f.get("parent")
                return str(folder) if folder else None
        return None

    def set_display_name(self, serial: str, name: str) -> None:
        self.session.request(
            "PUT", self.DEVICE_PATH.format(serial=serial), body={"display_name": name}
        )

    def deregister(self, serial: str) -> None:
        self.session.request("DELETE", self.DEVICE_PATH.format(serial=serial))


class ScmRuleClient:
    """RuleClient over the SCM REST API — the enrich step (ADR-0003).

    Writes the security-rule fields the `scm` Terraform provider silently drops
    (application / profile_setting / log_setting / ordering). Consumed by
    `fwgitops.enrich.enrich_folder`, which owns the GET-modify-PUT merge, the
    non-destructive opt-in semantics, and fail-closed behaviour.

    ✅ Paths CONFIRMED live (2026-07-28): a POST→GET→DELETE round-trip on
    `/config/security/v1/security-rules` set application/profile_setting/log_setting
    and read them back identical. Move path + body from pan.dev (move-security-rules-by-id):
    POST `.../{id}:move` {destination: top|bottom|before|after, rulebase: pre|post,
    destination_rule?}.
    """

    RULES_PATH = "/config/security/v1/security-rules"
    RULE_PATH = "/config/security/v1/security-rules/{id}"
    MOVE_PATH = "/config/security/v1/security-rules/{id}:move"

    def __init__(self, session: ScmSession, *, position: str = "pre"):
        self.session = session
        self.position = position  # rulebase the managed rules live in

    def rule_ids_by_name(self, folder: str) -> Dict[str, str]:
        """Map rule name -> UUID for a folder's rulebase (name = metadata.id)."""
        payload = self.session.request(
            "GET", self.RULES_PATH, params={"folder": folder, "position": self.position}
        )
        items = payload if isinstance(payload, list) else _first_present(payload, "data", default=[])
        out: Dict[str, str] = {}
        for r in items or []:
            if isinstance(r, dict) and r.get("name") and r.get("id"):
                out[str(r["name"])] = str(r["id"])
        return out

    def get_rule(self, rule_id: str) -> Dict[str, Any]:
        return self.session.request("GET", self.RULE_PATH.format(id=rule_id))

    def update_rule(self, rule_id: str, body: Dict[str, Any]) -> None:
        self.session.request("PUT", self.RULE_PATH.format(id=rule_id), body=body)

    def move_rule(
        self, rule_id: str, *, destination: str, rulebase: str, target: Optional[str] = None
    ) -> None:
        body: Dict[str, Any] = {"destination": destination, "rulebase": rulebase}
        if target:
            body["destination_rule"] = target
        self.session.request("POST", self.MOVE_PATH.format(id=rule_id), body=body)


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
