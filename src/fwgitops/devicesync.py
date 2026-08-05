"""Is the FIREWALL running what SCM holds?

THE GAP THIS CLOSES. Drift detection compares Git against SCM. Nothing compared
SCM against the DEVICE, so a change could be applied in SCM and never reach the
firewall — Git and SCM agreeing while the device runs something else. It is
silent, it persists, and the next successful push by anyone applies it, including
someone pushing an unrelated change.

That is not hypothetical. Testing `RouteRequest` deletion on 2026-08-06, the
logical router was destroyed in SCM and the push was refused, leaving SCM saying
"no default route" while the device still forwarded on one. Nothing in the
pipeline would have reported that.

── HOW SCM ANSWERS IT ────────────────────────────────────────────────────
Two documented endpoints (pan.dev, Configuration Operations → Config Versions):

    GET /config/operations/v1/config-versions/running     per-device running version
    GET /config/operations/v1/config-versions/candidate    committed version history

`running` returns `{"device": <serial>, "version": N, "date": ...}` per device.
`candidate` returns the folder's COMMITTED VERSION HISTORY — every past commit,
back months — which is emphatically NOT a list of pending edits. That
misreading has now cost this project twice: once when a "detect-drift" guard
refused forever after any human had ever committed (see push.py), and once on
2026-08-06 when it was read as "other admins have staged changes" and nearly led
to discarding a candidate that contained nothing of the sort.

The device is in sync when its running version equals the newest committed
version for its folder. The mapping is `running.version` <-> `candidate.id`,
evidenced on this tenant: version 70 running, candidate id 70, timestamps four
seconds apart. Stated here so it is falsifiable rather than assumed.

`is_first_push_done: false` is a THIRD state, and distinct: a device that has
never been pushed to in its CURRENT registration. A re-onboard resets it, and
SCM then refuses an admin-scoped partial push — it has no per-admin baseline to
diff against, so the first push must be a full one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

#: A device in sync, behind, or never pushed to.
IN_SYNC = "in-sync"
BEHIND = "behind"
NEVER_PUSHED = "never-pushed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeviceSync:
    serial: str
    folder: Optional[str]
    state: str
    running_version: Optional[int] = None
    latest_version: Optional[int] = None
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        return self.state in (BEHIND, NEVER_PUSHED, UNKNOWN)


def running_by_device(rows: Any) -> Dict[str, int]:
    """serial -> running config version, from the `running` endpoint payload."""
    out: Dict[str, int] = {}
    for r in (rows if isinstance(rows, list) else (rows or {}).get("data", []) or []):
        dev, ver = r.get("device"), r.get("version")
        if isinstance(dev, str) and isinstance(ver, int):
            out[dev] = ver
    return out


def latest_committed(versions: Iterable[dict]) -> Optional[int]:
    """Newest committed version id for a folder, or None if it has none."""
    ids = [v.get("id") for v in versions if isinstance(v.get("id"), int)]
    return max(ids) if ids else None


def compare(devices: Iterable[dict], running: Dict[str, int],
            latest_by_folder: Dict[str, Optional[int]]) -> List[DeviceSync]:
    """One verdict per device.

    `devices` is the `/config/setup/v1/devices` payload — it carries the folder
    and `is_first_push_done`, so the never-pushed case is read from SCM rather
    than inferred from a missing version.
    """
    out: List[DeviceSync] = []
    for d in devices:
        serial = d.get("serial_number") or d.get("name")
        if not isinstance(serial, str):
            continue
        folder = d.get("folder")

        if d.get("is_first_push_done") is False:
            out.append(DeviceSync(
                serial, folder, NEVER_PUSHED,
                running.get(serial), latest_by_folder.get(folder),
                "SCM reports is_first_push_done=false — this device has never been "
                "pushed to in its current registration (a re-onboard resets it). SCM "
                "refuses an ADMIN-SCOPED push in this state, because it has no "
                "per-admin baseline to diff against: the first push must be a full one."))
            continue

        latest = latest_by_folder.get(folder)
        ver = running.get(serial)
        if ver is None or latest is None:
            out.append(DeviceSync(
                serial, folder, UNKNOWN, ver, latest,
                "no running version reported for this device, or no committed version "
                "for its folder — cannot tell whether it is current. Treated as a "
                "problem rather than assumed fine."))
        elif ver < latest:
            out.append(DeviceSync(
                serial, folder, BEHIND, ver, latest,
                f"running config version {ver}, but folder {folder!r} has committed "
                f"version {latest}. Config exists in SCM that the firewall is NOT "
                f"enforcing — and the next successful push by anyone will apply it, "
                f"including someone pushing an unrelated change."))
        else:
            out.append(DeviceSync(serial, folder, IN_SYNC, ver, latest,
                                  "running the newest committed version"))
    return out
