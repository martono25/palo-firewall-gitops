"""Tests for device onboarding finalization (verify placement + set name)."""

from __future__ import annotations

import pytest

from fwgitops._poll import PollConfig
from fwgitops.onboard import OnboardResult, PlacementTimeout, deregister_device, onboard_device

NOSLEEP = lambda _s: None  # noqa: E731
FAST = PollConfig(max_attempts=5, backoff_seconds=0)
SERIAL = "007955000891682"


class FakeClient:
    def __init__(self, folders=None):
        # `folders`: value(s) returned by successive device_folder() calls.
        self.folders = folders if isinstance(folders, list) else [folders]
        self.calls = 0
        self.named = None
        self.deregistered: list = []

    def device_folder(self, serial):
        self.calls += 1
        return self.folders[min(self.calls - 1, len(self.folders) - 1)]

    def set_display_name(self, serial, name):
        self.named = (serial, name)

    def deregister(self, serial):
        self.deregistered.append(serial)


def run(client, **kw):
    kw.setdefault("expected_folder", "prod-edge")
    return onboard_device(client, SERIAL, poll=FAST, sleep=NOSLEEP, **kw)


def test_verify_placement_then_name():
    c = FakeClient(folders="prod-edge")
    r = run(c, display_name="fw-prod-edge-682")
    assert r == OnboardResult(SERIAL, "prod-edge", "fw-prod-edge-682")
    assert c.named == (SERIAL, "fw-prod-edge-682")


def test_waits_for_placement_to_land():
    c = FakeClient(folders=[None, "prod-edge", "prod-edge"])  # not placed, then lands
    assert run(c).folder == "prod-edge"
    assert c.calls == 2


def test_no_display_name_skips_naming():
    c = FakeClient(folders="prod-edge")
    r = run(c)
    assert r.display_name is None
    assert c.named is None


def test_placement_timeout_raises_and_never_names():
    c = FakeClient(folders=None)  # never lands
    with pytest.raises(PlacementTimeout):
        run(c, display_name="x")
    assert c.named is None  # critical: never name a device that isn't placed


def test_wrong_folder_is_not_placement():
    c = FakeClient(folders="Available Devices")  # landed somewhere else
    with pytest.raises(PlacementTimeout):
        run(c)


def test_evidence_shape():
    ev = run(FakeClient(folders="prod-edge"), display_name="n").to_evidence()
    assert ev == {"serial": SERIAL, "folder": "prod-edge", "display_name": "n"}


def test_deregister_calls_client():
    c = FakeClient()
    deregister_device(c, SERIAL)
    assert c.deregistered == [SERIAL]
