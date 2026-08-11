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
    BEHIND, FIRST_PUSH_PENDING, IN_SYNC, UNKNOWN, compare, latest_committed,
    running_by_device,
)

DEV = {"serial_number": "007955000901881", "name": "007955000901881",
       "folder": "prod-edge", "is_first_push_done": True}


def test_a_device_behind_its_folder_is_flagged():
    """The case that motivated this: config exists in SCM that the firewall is
    not enforcing, and the next successful push by ANYONE applies it — including
    someone pushing something unrelated."""
    r = compare([DEV], {"007955000901881": 68}, {"prod-edge": 70})[0]
    assert r.state == BEHIND and r.is_problem
    assert "next successful push by anyone" in r.detail


def test_a_device_running_the_newest_version_is_clean():
    r = compare([DEV], {"007955000901881": 70}, {"prod-edge": 70})[0]
    assert r.state == IN_SYNC and not r.is_problem


def test_first_push_pending_is_a_NOTE_not_a_failure():
    """CORRECTION to v1.31.0, measured rather than assumed.

    `is_first_push_done` was treated as a sync signal. On this tenant it stayed
    `false` across TWO successful pushes (jobs 170 and 172, both CommitAndPush /
    FIN / OK, running version advancing v70 -> v71 -> v72) while the device was
    verified over SSH to be running exactly the intended config.

    So a device can be demonstrably current and still report false. Blocking on
    it is a FALSE POSITIVE on a healthy firewall, which is how a check gets
    ignored. Still reported, because SCM refuses an ADMIN-SCOPED push while it is
    false — real, and not the same as "running stale config".
    """
    dev = {**DEV, "is_first_push_done": False}
    r = compare([dev], {"007955000901881": 70}, {"prod-edge": 70})[0]
    assert r.state == FIRST_PUSH_PENDING
    assert not r.is_problem, "a current firewall must not be reported as out of sync"
    assert r.is_note
    assert "the firewall is CURRENT" in r.detail


def test_behind_wins_over_first_push_pending():
    """If the version really is behind, that is the finding — the flag must not
    downgrade a genuinely stale firewall to a note."""
    dev = {**DEV, "is_first_push_done": False}
    r = compare([dev], {"007955000901881": 68}, {"prod-edge": 70})[0]
    assert r.state == BEHIND and r.is_problem


def test_a_missing_version_is_UNKNOWN_not_assumed_fine():
    """Fail closed. "No data" and "up to date" must not look the same."""
    assert compare([DEV], {}, {"prod-edge": 70})[0].state == UNKNOWN
    assert compare([DEV], {"007955000901881": 70}, {})[0].state == UNKNOWN
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
        [DEV], [{"device": "007955000901881", "version": 68}], [{"id": 70}]))
    assert rc == 2
    assert "OUT OF SYNC" in capsys.readouterr().err


def test_the_command_exits_0_when_everything_is_current(capsys):
    rc = run_device_sync(session=_Session(
        [DEV], [{"device": "007955000901881", "version": 70}], [{"id": 70}]))
    assert rc == 0
    assert "running the newest committed config" in capsys.readouterr().out


def test_the_command_exits_0_for_first_push_pending_but_says_so(capsys):
    """The real pilot state after re-onboarding: current, but SCM will refuse an
    admin-scoped push. Worth printing, not worth failing a scheduled job over."""
    dev = {**DEV, "is_first_push_done": False}
    rc = run_device_sync(session=_Session(
        [dev], [{"device": "007955000901881", "version": 72}], [{"id": 72}]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOTE" in out and "is_first_push_done" in out


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


# ── state drift at DEVICE scope ───────────────────────────────────────────
def test_a_device_scope_row_defines_at_device_not_in_a_folder(tmp_path, capsys):
    """A device-scope OVERRIDE is defined at `device:<serial>` and the snapshot
    row carries `device` with NO `folder` at all. Requiring `folder` rejected
    every device snapshot outright:

        error: ... [0] must have 'folder' and 'name'

    so state drift was never checked for a firewall's own overrides.
    """
    import json
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
    from fwgitops.cli import run_drift

    snap = tmp_path / "s.json"
    snap.write_text(json.dumps([{
        "kind": "InterfaceRequest", "name": "ethernet1/2",
        "device": "007955000901881", "scope": "device:007955000901881",
        "layer3": {}}]))
    env = tmp_path / "env.yaml"
    env.write_text("prod:\n  folder: prod-edge\n  from_zone: l\n  to_zone: i\n")
    (tmp_path / "intent").mkdir()

    rc = run_drift(tmp_path / "intent", env, state_snapshot_paths=[snap])
    err = capsys.readouterr().err
    assert "must have 'folder' and 'name'" not in err, \
        "a device row has no folder — that is not malformed"
    assert rc in (0, 3), f"expected a drift verdict, got rc={rc}: {err}"


def test_only_the_scopes_the_snapshot_COVERS_are_compared(tmp_path, capsys):
    """The caller checks one root at a time — the scheduled job loops
    `terraform/*/` — so a device snapshot contains nothing about `prod-edge`.
    Comparing the whole declared set against it reported every other scope's
    objects as "declared in Git, absent from SCM": drift that is not there, on a
    firewall perfectly in step."""
    from fwgitops.drift import ActualObject, detect_object_drift

    declared = {
        ("device:007955000901881", "ethernet1/2"): {"name": "ethernet1/2"},
        ("prod-edge", "dmz"): {"name": "dmz"},
    }
    actual = [ActualObject(kind="InterfaceRequest", folder="device:007955000901881",
                           name="ethernet1/2", scope="device:007955000901881",
                           fields={"name": "ethernet1/2"})]
    covered = {a.scope_folder for a in actual}
    scoped = {k: v for k, v in declared.items() if k[0] in covered}
    assert detect_object_drift(scoped, actual).is_clean
    # and without the filter, the untouched folder object looks missing
    assert not detect_object_drift(declared, actual).is_clean
