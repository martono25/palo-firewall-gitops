"""SCM push step — the atomic commit boundary (T13).

Spike finding (2026-07-19): the `scm` Terraform provider has NO push/commit
capability. `terraform apply` only STAGES configuration in SCM; a separate push
makes it live, and **SCM's push target is the FOLDER**. So the candidate/commit
boundary from the design is structurally forced, and this module is it.

    terraform apply ──▶ config staged in SCM
                            │
        push_folder() ──────┴──▶ live on devices   ← the atomic point

Because push is folder-scoped it commits EVERYTHING staged in that folder, not
just our change. That is dangerous: an out-of-band GUI edit sitting in the same
folder would go live under our change's audit trail, unreviewed. So this module
**fails closed** — if anything unexpected is staged, it refuses to push and
reports the delta as Level-1 drift for the drift flow to handle.

Ordering of guarantees:
  1. list what is actually staged
  2. refuse to push if that exceeds what we staged   ← fail closed
  3. push (target = folder)
  4. poll the push job to completion, bounded
  5. return a result suitable for the evidence bundle

SCM calls sit behind the `PushClient` protocol so the logic is unit-testable;
the real client (SCM REST API) is thin glue on top.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, Optional, Sequence, Tuple

from fwgitops._poll import PollConfig, bounded_poll

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore


class PushStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


TERMINAL = (PushStatus.SUCCESS, PushStatus.FAILED)


@dataclass(frozen=True)
class JobState:
    status: PushStatus
    message: str = ""


class PushError(Exception):
    """Base class for push failures."""


class UnexpectedStagedChanges(PushError):
    """Refused to push: the folder holds staged changes we did not make.

    This is the fail-closed guard. The extra delta is out-of-band (Level-1)
    drift — it must go through the drift flow, not be silently committed under
    this change's audit trail.
    """

    def __init__(self, folder: str, unexpected: Sequence[str]):
        self.folder = folder
        self.unexpected = tuple(unexpected)
        super().__init__(
            f"refusing to push folder {folder!r}: {len(self.unexpected)} unexpected staged "
            f"change(s) not made by this run: {', '.join(self.unexpected)}. "
            "Resolve as drift before pushing."
        )


class PushTimeout(PushError):
    """The push job did not reach a terminal state within the poll budget."""


class PushFailed(PushError):
    """SCM reported the push job as failed."""


class PushClient(Protocol):
    """SCM operations this step drives (real impl = SCM REST API)."""

    def list_staged(self, folder: str) -> Iterable[str]: ...
    def push(self, folder: str) -> str: ...
    def job_status(self, job_id: str) -> JobState: ...


@dataclass(frozen=True)
class PushResult:
    """Outcome of a push, shaped for the evidence bundle."""

    folder: str
    status: str  # "success" | "noop"
    job_id: Optional[str]
    pushed: Tuple[str, ...]
    missing: Tuple[str, ...]  # expected but not staged (informational)

    def to_evidence(self) -> Dict[str, object]:
        return {
            "folder": self.folder,
            "status": self.status,
            "job_id": self.job_id,
            "pushed": list(self.pushed),
            "missing": list(self.missing),
        }


def push_folder(
    client: PushClient,
    folder: str,
    *,
    expected: Iterable[str],
    poll: PollConfig = PollConfig(),
    sleep: Callable[[float], None] = time.sleep,
    allow_unexpected: bool = False,
) -> PushResult:
    """Push a folder's staged config, failing closed on anything unexpected.

    `expected` is the set of object/rule identifiers this run staged (derived
    from the compiled desired-state). Anything staged beyond that aborts the push.

    `allow_unexpected=True` is an explicit break-glass override — it should only
    ever be set by a human-approved run, never by the default pipeline path.
    """
    staged = set(client.list_staged(folder))
    expected_set = set(expected)

    unexpected = staged - expected_set
    if unexpected and not allow_unexpected:
        raise UnexpectedStagedChanges(folder, sorted(unexpected))

    if not staged:
        # Apply was a no-op (or SCM already committed it) — nothing to push.
        return PushResult(
            folder=folder,
            status="noop",
            job_id=None,
            pushed=(),
            missing=tuple(sorted(expected_set)),
        )

    job_id = client.push(folder)
    state = bounded_poll(
        lambda: (lambda s: s if s.status in TERMINAL else None)(client.job_status(job_id)),
        poll,
        sleep,
    )
    if state is None:
        raise PushTimeout(
            f"push job {job_id!r} for folder {folder!r} did not finish after "
            f"{poll.max_attempts} attempts"
        )
    if state.status is PushStatus.FAILED:
        raise PushFailed(f"push job {job_id!r} for folder {folder!r} failed: {state.message}")

    return PushResult(
        folder=folder,
        status="success",
        job_id=job_id,
        pushed=tuple(sorted(staged)),
        missing=tuple(sorted(expected_set - staged)),
    )
