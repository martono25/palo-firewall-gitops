"""Device onboarding finalization (post auto-placement).

The serial-regex onboarding rule auto-places a fresh VM-Series into its target
folder at first connect (proven 2026-07-26). This module does the remaining,
API-driven finalization that the POLICY service account is allowed to do:

  1. verify the device landed in the expected folder   (GET /config/setup/v1/folders)
  2. set a human-readable display name                 (PUT /config/setup/v1/devices/{serial})

and the mirror teardown step:

  3. deregister the device                             (DELETE /config/setup/v1/devices/{serial})

`terraform destroy` tears down the VM but leaves the SCM device registered
(finding 2026-07-26: destroy != deregister), and a folder push targets ALL
devices in the folder — so a stale registration breaks later pushes. Teardown
must call deregister explicitly.

Privilege note: onboarding RULES and device general-settings are denied to the
policy SA, but `/folders` (read) and `/devices/{serial}` (PUT/DELETE) are not
(verified live). So this whole flow runs as the policy SA — no elevated identity.

Serial capture (`ssh admin@<mgmt_ip> 'show system info'` -> the `serial:` field)
is a provisioning-host concern; the serial is an INPUT here. SCM calls sit behind
the `DeviceClient` protocol so the orchestration is unit-testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from fwgitops._poll import PollConfig, bounded_poll

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore


class OnboardError(Exception):
    """Base class for onboarding-finalization failures."""


class PlacementTimeout(OnboardError):
    """The device did not appear in the expected folder within the poll budget."""


class DeviceClient(Protocol):
    """SCM device operations this step drives (real impl = SCM REST API)."""

    def device_folder(self, serial: str) -> Optional[str]: ...
    def set_display_name(self, serial: str, name: str) -> None: ...
    def deregister(self, serial: str) -> None: ...


@dataclass(frozen=True)
class OnboardResult:
    """Outcome of onboarding finalization, shaped for the evidence bundle."""

    serial: str
    folder: str
    display_name: Optional[str]

    def to_evidence(self) -> Dict[str, object]:
        return {"serial": self.serial, "folder": self.folder, "display_name": self.display_name}


def onboard_device(
    client: DeviceClient,
    serial: str,
    *,
    expected_folder: str,
    display_name: Optional[str] = None,
    poll: PollConfig = PollConfig(),
    sleep: Callable[[float], None] = time.sleep,
) -> OnboardResult:
    """Finalize onboarding: confirm placement, then set the display name.

    Waits (bounded) for the auto-placement to land the device in
    `expected_folder`, then — if `display_name` is given — sets the SCM display
    name. Fails closed: if placement never confirms, raises rather than naming a
    device that isn't where we think it is.
    """
    landed = bounded_poll(
        lambda: (lambda f: f if f == expected_folder else None)(client.device_folder(serial)),
        poll,
        sleep,
    )
    if landed is None:
        seen = client.device_folder(serial)
        raise PlacementTimeout(
            f"device {serial!r} not in folder {expected_folder!r} after "
            f"{poll.max_attempts} attempts (currently: {seen!r}). Check the "
            f"onboarding rule's serial match and that the device connected."
        )

    if display_name is not None:
        client.set_display_name(serial, display_name)

    return OnboardResult(serial=serial, folder=expected_folder, display_name=display_name)


def deregister_device(client: DeviceClient, serial: str) -> None:
    """Remove a device's SCM registration (teardown; mirror of onboarding).

    Call this when decommissioning — `terraform destroy` does NOT deregister, and
    a leftover registration in a folder breaks that folder's future pushes.
    """
    client.deregister(serial)
