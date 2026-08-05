"""Is the FIREWALL running what SCM holds?

Drift compares Git against SCM. Nothing compared SCM against the DEVICE, so a
change could be applied in SCM and never reach the firewall — Git and SCM
agreeing while the device runs something else.

Not hypothetical: testing RouteRequest deletion on 2026-08-06, the logical router
was destroyed in SCM and the push was refused, leaving SCM saying "no default
route" while the device still forwarded on one. Nothing reported it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.cli import run_device_sync  # noqa: E402
from fwgitops.devicesync import (  # noqa: E402
    BEHIND, IN_SYNC, NEVER_PUSHED, UNKNOWN, compare, latest_committed, running_by_device,
)

DEV = {"serial_number": "007955000894453", "name": "007955000894453",
       "folder": "prod-edge", "is_first_push_done": True}


def test_a_device_behind_its_folder_is_flagged():
    """The case that motivated this: config exists in SCM that the firewall is
    not enforcing, and the next successful push by ANYONE applies it — including
    someone pushing something unrelated."""
    r = compare([DEV], {"007955000894453": 68}, {"prod-edge": 70})[0]
    assert r.state == BEHIND and r.is_problem
    assert "next successful push by anyone" in r.detail


def test_a_device_running_the_newest_version_is_clean():
    r = compare([DEV], {"007955000894453": 70}, {"prod-edge": 70})[0]
    assert r.state == IN_SYNC and not r.is_problem


def test_never_pushed_beats_a_matching_version():
    """A re-onboard resets `is_first_push_done` while the OLD running version
    remains. Comparing versions alone would call that in-sync — it is not: SCM
    has no per-admin baseline for the device and refuses an admin-scoped push,
    which is exactly what blocked the route-deletion test."""
    dev = {**DEV, "is_first_push_done": False}
    r = compare([dev], {"007955000894453": 70}, {"prod-edge": 70})[0]
    assert r.state == NEVER_PUSHED and r.is_problem
    assert "must be a full one" in r.detail


def test_a_missing_version_is_UNKNOWN_not_assumed_fine():
    """Fail closed. "No data" and "up to date" must not look the same."""
    assert compare([DEV], {}, {"prod-edge": 70})[0].state == UNKNOWN
    assert compare([DEV], {"007955000894453": 70}, {})[0].state == UNKNOWN
    assert all(compare([DEV], {}, {})[0].is_problem for _ in (1,))


def test_running_payload_parsing_ignores_malformed_rows():
    rows = [{"device": "A", "version": 3}, {"device": None, "version": 4},
            {"device": "B"}, {"device": "C", "version": "x"}]
    assert running_by_device(rows) == {"A": 3}
    assert running_by_device({"data": rows}) == {"A": 3}


def test_latest_committed_picks_the_highest_id():
    assert latest_committed([{"id": 4}, {"id": 70}, {"id": 12}]) == 70
    assert latest_committed([]) is None


# ── the command ───────────────────────────────────────────────────────────
class _Session:
    def __init__(self, devices, running, candidates):
        self._d, self._r, self._c = devices, running, candidates

    def request(self, method, path, params=None, body=None):
        if path.endswith("/devices"):
            return {"data": self._d}
        if path.endswith("/running"):
            return self._r
        if path.endswith("/candidate"):
            return {"data": self._c}
        raise AssertionError(path)


def test_the_command_exits_2_when_a_device_is_behind(capsys):
    rc = run_device_sync(session=_Session(
        [DEV], [{"device": "007955000894453", "version": 68}], [{"id": 70}]))
    assert rc == 2
    assert "OUT OF SYNC" in capsys.readouterr().err


def test_the_command_exits_0_when_everything_is_current(capsys):
    rc = run_device_sync(session=_Session(
        [DEV], [{"device": "007955000894453", "version": 70}], [{"id": 70}]))
    assert rc == 0
    assert "running the newest committed config" in capsys.readouterr().out


def test_an_empty_inventory_is_an_ERROR_not_a_pass(capsys):
    """No devices could mean a healthy empty tenant OR a broken read. Reporting
    "all in sync" for the second is the blindness this command removes."""
    rc = run_device_sync(session=_Session([], [], []))
    assert rc == 1
    assert "refusing to report sync status" in capsys.readouterr().err


def test_a_read_failure_is_an_ERROR_not_a_pass(capsys):
    class _Broken:
        def request(self, *a, **k):
            raise RuntimeError("connection reset")
    assert run_device_sync(session=_Broken()) == 1
    assert "connection reset" in capsys.readouterr().err
