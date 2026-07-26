"""SCM push step — the atomic commit boundary (T13).

Spike finding (2026-07-19): the `scm` Terraform provider has NO push/commit
capability. `terraform apply` only STAGES configuration in SCM; a separate push
makes it live, and **SCM's push target is the FOLDER**. So the candidate/commit
boundary from the design is structurally forced, and this module is it.

    terraform apply ──▶ config staged in SCM
                            │
        push_folder() ──────┴──▶ live on devices   ← the atomic point

SCM has a SHARED candidate: a folder-scoped push would otherwise commit EVERY
editor's staged changes — an out-of-band GUI edit would go live under our audit
trail, unreviewed. We fail safe by **admin-scoped partial push**: the push
request's `admin` field scopes the commit to just our service account's staged
changes, so foreign pending edits are never swept in (safe by construction).

This replaced an earlier "detect-drift" guard that read the candidate's editor
list to refuse on out-of-band edits. That signal was WRONG: SCM's
`config-versions/candidate` returns committed version HISTORY (every past
committer, back months), not current pending drift — so it refused forever once
any human had ever committed in the folder. Scoping the commit is both correct
and simpler: we don't detect drift, we just never commit anyone else's work.

Ordering of guarantees:
  1. push, scoped to our service account (target = folder)   ← safe by construction
  2. poll the push job to completion, bounded
  3. return a result suitable for the evidence bundle

`all_admins=True` is the break-glass: push the WHOLE candidate (baseline
absorption / an approved manual run), never the default pipeline path.

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


class PushTimeout(PushError):
    """The push job did not reach a terminal state within the poll budget."""


class PushFailed(PushError):
    """SCM reported the push job as failed."""


class PushClient(Protocol):
    """SCM operations this step drives (real impl = SCM REST API)."""

    def push(self, folder: str, *, admins: Optional[Sequence[str]]) -> Optional[str]: ...
    def job_status(self, job_id: str) -> JobState: ...


@dataclass(frozen=True)
class PushResult:
    """Outcome of a push, shaped for the evidence bundle."""

    folder: str
    status: str  # "success" | "noop"
    job_id: Optional[str]
    admins: Tuple[str, ...]  # identities the commit was scoped to (audit); () = unscoped

    def to_evidence(self) -> Dict[str, object]:
        return {
            "folder": self.folder,
            "status": self.status,
            "job_id": self.job_id,
            "admins": list(self.admins),
        }


def push_folder(
    client: PushClient,
    folder: str,
    *,
    admins: Sequence[str],
    poll: PollConfig = PollConfig(),
    sleep: Callable[[float], None] = time.sleep,
    all_admins: bool = False,
) -> PushResult:
    """Push a folder's staged config, scoped to our service account.

    `admins` is the set of identities whose staged changes to commit — normally
    just our service account. Scoping the commit means a shared-candidate folder
    with out-of-band edits is safe: we only ever commit our own staged work, so
    there is no drift to detect and no false refusal (see module docstring).

    `all_admins=True` is the break-glass: commit the WHOLE candidate regardless
    of who staged it (baseline absorption / an approved manual run). It maps to
    an unscoped push (no `admin` field).
    """
    scope: Optional[Sequence[str]] = None if all_admins else list(admins)

    job_id = client.push(folder, admins=scope)
    if job_id is None:
        # Nothing staged for this scope — a steady-state no-op, not a failure.
        return PushResult(folder=folder, status="noop", job_id=None, admins=tuple(scope or ()))

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
        folder=folder, status="success", job_id=job_id, admins=tuple(scope or ())
    )
