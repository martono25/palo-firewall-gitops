"""Shared bounded-poll helper.

Both Day-1 provisioning ("wait until the device is connected") and the Day-2
SCM push ("wait until the push job finishes") need the same shape: probe a
remote thing on a bounded schedule, with an injected sleep so tests don't wait.
Defined once here rather than duplicated in each module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class PollConfig:
    max_attempts: int = 30
    backoff_seconds: float = 10.0


def bounded_poll(
    probe: Callable[[], Optional[T]],
    poll: PollConfig,
    sleep: Callable[[float], None],
) -> Optional[T]:
    """Call `probe` up to `poll.max_attempts` times.

    Returns the first non-None result, or None if the budget is exhausted — the
    caller decides what timing out means and raises its own error.
    """
    for attempt in range(1, poll.max_attempts + 1):
        result = probe()
        if result is not None:
            return result
        if attempt < poll.max_attempts:
            sleep(poll.backoff_seconds)
    return None
