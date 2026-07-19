"""Day-1 provisioning orchestration — re-entrant onboarding state machine (T3).

Terraform instantiates the NGFW (compute/networking/bootstrap inputs); this
module drives everything after: licensing, SCM onboarding, baseline, and the
"wait until connected" verification. The design calls for it to be **re-entrant**
(safe to re-run from any point) with a **bounded poll** and a **retry loop around
licensing** (the flaky step). See docs/DESIGN.md → Day-1 Provisioning.

Re-entrancy is achieved by deriving the current stage from *reality* (an SCM/
cloud query) on every call and advancing one step at a time, rather than trusting
local state. A run that dies mid-onboard resumes cleanly on the next invocation.

    current_stage() ─▶ advance one step ─▶ re-check ─▶ … ─▶ READY

The SCM/cloud calls are behind the `ProvisionClient` protocol so the orchestration
is unit-testable with a fake; the real client (SCM REST API) is the unvalidated
part built during the spike. `sleep` is injected so tests don't wait.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

try:  # Protocol is stdlib on 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore


class Stage(IntEnum):
    """Ordered provisioning stages. Comparable so we can 'advance to' a stage."""

    ABSENT = 0        # not instantiated (Terraform must create it first)
    INSTANTIATED = 1  # VM/HW exists and is reachable, not yet licensed
    LICENSED = 2      # licenses/subscriptions active
    ONBOARDED = 3     # claimed into its SCM folder
    BASELINED = 4     # baseline snippet applied
    READY = 5         # baselined + connected/in-sync


class ProvisionError(Exception):
    """Provisioning could not complete."""


class ProvisionTimeout(ProvisionError):
    """Device did not reach a connected state within the poll budget."""


class LicenseActivationError(ProvisionError):
    """License/subscription activation failed (often transient — retried)."""


class ProvisionClient(Protocol):
    """The SCM/cloud operations the orchestrator drives (real impl = SCM API)."""

    def current_stage(self, device_id: str) -> Stage: ...
    def activate_license(self, device_id: str) -> None: ...
    def onboard(self, device_id: str, folder: str) -> None: ...
    def apply_baseline(self, device_id: str, snippet: str) -> None: ...
    def is_connected(self, device_id: str) -> bool: ...


@dataclass(frozen=True)
class PollConfig:
    max_attempts: int = 30
    backoff_seconds: float = 10.0


def provision(
    client: ProvisionClient,
    device_id: str,
    *,
    folder: str,
    snippet: str,
    license_retries: int = 5,
    poll: PollConfig = PollConfig(),
    sleep: Callable[[float], None] = time.sleep,
) -> Stage:
    """Drive `device_id` to READY, resuming from whatever stage it is really in.

    Idempotent and re-entrant: calling again after completion is a no-op; calling
    after a partial run resumes from the current stage without redoing earlier steps.
    """
    # Guard against a misbehaving client that never advances (belt-and-suspenders).
    for _ in range(len(Stage) + 2):
        stage = client.current_stage(device_id)

        if stage >= Stage.READY:
            return Stage.READY
        if stage < Stage.INSTANTIATED:
            raise ProvisionError(
                f"{device_id!r} is not instantiated — Terraform must create it before onboarding"
            )
        if stage < Stage.LICENSED:
            _activate_with_retry(client, device_id, license_retries, poll.backoff_seconds, sleep)
            continue
        if stage < Stage.ONBOARDED:
            client.onboard(device_id, folder)
            continue
        if stage < Stage.BASELINED:
            client.apply_baseline(device_id, snippet)
            continue
        # stage == BASELINED: confirm it actually connected/synced, then done.
        _wait_connected(client, device_id, poll, sleep)
        return Stage.READY

    raise ProvisionError(f"{device_id!r} did not converge to READY (client not advancing?)")


def _activate_with_retry(
    client: ProvisionClient,
    device_id: str,
    retries: int,
    backoff: float,
    sleep: Callable[[float], None],
) -> None:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client.activate_license(device_id)
            return
        except LicenseActivationError as e:
            last = e
            if attempt < retries:
                sleep(backoff)
    raise LicenseActivationError(
        f"license activation for {device_id!r} failed after {retries} attempts: {last}"
    )


def _wait_connected(
    client: ProvisionClient,
    device_id: str,
    poll: PollConfig,
    sleep: Callable[[float], None],
) -> None:
    for attempt in range(1, poll.max_attempts + 1):
        if client.is_connected(device_id):
            return
        if attempt < poll.max_attempts:
            sleep(poll.backoff_seconds)
    raise ProvisionTimeout(
        f"{device_id!r} not connected after {poll.max_attempts} attempts"
    )
