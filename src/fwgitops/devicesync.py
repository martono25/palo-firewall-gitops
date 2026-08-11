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

`is_first_push_done` IS NOT A SYNC SIGNAL — measured, not assumed. It was
treated as one in v1.31.0 and that was wrong. On this tenant it stayed `false`
across TWO successful pushes (folder-scoped job 170, device-scoped job 172,
both `CommitAndPush` / `FIN` / `OK`, running version advancing v70 -> v71 -> v72),
while the device was verified over SSH to be running exactly the intended config.
`last_device_update_time` never moved either.

So a device can be demonstrably current and still report `is_first_push_done:
false`. Blocking on it produced a FALSE POSITIVE on a healthy firewall, which is
how a check gets ignored — the same reasoning that keeps `targetable: false` an
acknowledgement in `verify-catalog`.

It is still reported, but the "SCM refuses an admin-scoped push while it is
false" claim that used to sit here DID NOT HOLD. Measured 2026-08-11 on a
freshly provisioned firewall (007955000901881) reporting
`is_first_push_done: false`: the pipeline's normal ADMIN-SCOPED push succeeded
first time — device-scope job 202, three interfaces committed and verified on
the device. Admin-scoped pushes to the previous firewall had also succeeded
while the flag was false.

So the flag correlates with nothing this pipeline needs to act on. It is
reported because it is a state SCM exposes and an operator will see it, not
because it predicts a failure — and a note that predicts one that never comes
is how a real warning gets ignored.

THE AUTHORITATIVE SIGNAL IS THE VERSION COMPARISON.

── WHAT THIS DOES NOT CATCH (measured 2026-08-06) ────────────────────────
An APPLIED-BUT-UNPUSHED change is INVISIBLE here. Terraform writes to SCM's
CANDIDATE; only a push commits it and creates a new version. So during the
route-deletion test the router was destroyed in SCM and this command still
reported `running=v72 committed=v72` — current — because no version had been
created for the pending destroy.

That is narrower than the gap this module's header claims to close, and the
difference matters:

  * CAUGHT   — committed but not delivered: a push created version N+1 and the
               device is still on N (offline during the push, partial delivery).
  * MISSED   — applied in SCM, never pushed: candidate differs from running, no
               version exists to compare.

The missed case is largely covered elsewhere by construction: apply.yml pushes
immediately after applying, so a refused push FAILS THE JOB loudly. It bites for
out-of-band applies — a human running `terraform apply` by hand, which is exactly
what produced it during the test.

Closing it properly needs a candidate-vs-running comparison, and SCM's
`config-versions/candidate` cannot supply one: it is version history. Recorded in
TODOS rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

#: A device in sync, behind, or never pushed to.
IN_SYNC = "in-sync"
BEHIND = "behind"
#: Current by version, but SCM has not recorded a first push. Reported, NOT a
#: failure: measured to persist across successful pushes (see the module note).
FIRST_PUSH_PENDING = "first-push-pending"
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
        return self.state in (BEHIND, UNKNOWN)

    @property
    def is_note(self) -> bool:
        """True for something worth saying that is not the firewall being stale."""
        return self.state == FIRST_PUSH_PENDING


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
        elif d.get("is_first_push_done") is False:
            out.append(DeviceSync(
                serial, folder, FIRST_PUSH_PENDING, ver, latest,
                f"running the newest committed version (v{ver}) — the firewall is "
                f"CURRENT — but SCM still reports is_first_push_done=false. On this "
                f"tenant that flag predicts nothing: it did not clear after several "
                f"successful pushes, and an ADMIN-SCOPED push to a fresh firewall "
                f"reporting false succeeded first time (2026-08-11, job 202). "
                f"Reported because SCM exposes it and you will see it, NOT because "
                f"it blocks anything."))
        else:
            out.append(DeviceSync(serial, folder, IN_SYNC, ver, latest,
                                  "running the newest committed version"))
    return out
