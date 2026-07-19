"""Tests for the re-entrant provisioning orchestration (T3).

The SCM/cloud client is faked; we assert the orchestration's behavior:
resumes from the real stage, retries the flaky license step, and bounds the
connect poll. `sleep` is a no-op so tests are instant.
"""

from __future__ import annotations

import pytest

from fwgitops.provision import (
    LicenseActivationError,
    PollConfig,
    ProvisionError,
    ProvisionTimeout,
    Stage,
    provision,
)

NOSLEEP = lambda _s: None  # noqa: E731


class FakeClient:
    """Models a device advancing through stages as operations are applied."""

    def __init__(self, start=Stage.INSTANTIATED, license_fail=0, connect_after=1):
        self.stage = start
        self.license_fail = license_fail
        self.connect_after = connect_after
        self.connect_calls = 0
        self.calls: list = []

    def current_stage(self, device_id):
        return self.stage

    def activate_license(self, device_id):
        self.calls.append("license")
        if self.license_fail > 0:
            self.license_fail -= 1
            raise LicenseActivationError("flaky CSP")
        self.stage = Stage.LICENSED

    def onboard(self, device_id, folder):
        self.calls.append(("onboard", folder))
        self.stage = Stage.ONBOARDED

    def apply_baseline(self, device_id, snippet):
        self.calls.append(("baseline", snippet))
        self.stage = Stage.BASELINED

    def is_connected(self, device_id):
        self.connect_calls += 1
        return self.connect_calls >= self.connect_after


def run(client, **kw):
    return provision(
        client, "vm-pilot-01", folder="prod-edge", snippet="baseline-v1",
        sleep=NOSLEEP, **kw,
    )


def test_full_provision_in_order():
    c = FakeClient(start=Stage.INSTANTIATED)
    assert run(c) == Stage.READY
    assert c.calls == ["license", ("onboard", "prod-edge"), ("baseline", "baseline-v1")]


def test_reentrant_resume_from_onboarded_skips_earlier_steps():
    c = FakeClient(start=Stage.ONBOARDED)
    assert run(c) == Stage.READY
    # License + onboard already done — only baseline runs on resume.
    assert c.calls == [("baseline", "baseline-v1")]


def test_already_ready_is_noop():
    c = FakeClient(start=Stage.READY)
    assert run(c) == Stage.READY
    assert c.calls == []


def test_not_instantiated_raises():
    c = FakeClient(start=Stage.ABSENT)
    with pytest.raises(ProvisionError, match="not instantiated"):
        run(c)


def test_license_retry_then_succeeds():
    c = FakeClient(start=Stage.INSTANTIATED, license_fail=2)
    assert run(c, license_retries=5) == Stage.READY
    assert c.calls.count("license") == 3  # 2 failures + 1 success


def test_license_exhausted_raises():
    c = FakeClient(start=Stage.INSTANTIATED, license_fail=99)
    with pytest.raises(LicenseActivationError, match="after 3 attempts"):
        run(c, license_retries=3)


def test_poll_timeout_when_never_connects():
    c = FakeClient(start=Stage.BASELINED, connect_after=999)
    with pytest.raises(ProvisionTimeout, match="not connected after 3"):
        run(c, poll=PollConfig(max_attempts=3, backoff_seconds=0))


def test_poll_succeeds_after_a_few_attempts():
    c = FakeClient(start=Stage.BASELINED, connect_after=3)
    assert run(c, poll=PollConfig(max_attempts=5, backoff_seconds=0)) == Stage.READY
