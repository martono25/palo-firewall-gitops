"""A detected violation is a RECORD, not a log line.

WHY THIS EXISTS. Drift detection failed the nightly run and left nothing behind.
The classification already existed in code — `unmanaged`, `orphaned`,
`malformed` — and never reached disk, so a finding could not be routed into a
follow-up process, counted, aged, or produced for an assessor six months later.
The CI log is not an audit trail: it expires, and the assessor guide says so.

WHAT A VIOLATION IS, precisely. Each class is a different failure of
authorisation, and they are NOT interchangeable in a report:

  * `unmanaged` — an object exists that this platform never created and no
    request authorised. Someone changed the firewall outside the process.
  * `malformed` — an object carries the `gitops:managed` marker but does not
    trace to the request it claims: either no `gitops:req` tag at all, or a
    `gitops:req` naming a DIFFERENT request from the object's own name. It
    CLAIMS this platform's provenance while tracing to no request of its own,
    which is worse than an honest stranger: it would pass a check that only
    looked for the marker. The second form is what a console COPY of a managed
    rule produces — it inherits both tags, so only the name gives it away.
  * `orphaned` — a managed object still live in SCM whose request is gone from
    Git. Authorised once, no longer declared.
  * `reordered` — a managed rule sits somewhere other than its deployed
    position. No rule was added, removed or edited, and every one of them is
    authorised; what changed is which rule matches FIRST, which is the policy.

FINDINGS, NOT EVENTS. A record is keyed on the OBJECT, not the run, so the same
violation detected nightly for a week is one record with `first_seen` and
`last_seen` — not seven. That distinction is what makes ageing possible ("open
for six days") and what stops a real finding drowning in duplicates.

A record is never deleted when the violation goes away. It is marked `resolved`,
with the timestamp, because "this was open for six days in August" is exactly
what a follow-up process needs to see afterwards.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = "fw-violation/v1"

#: Ordered by how much explaining they need, worst first. Used for reporting.
#:
#: `reordered` ranks second because it is the quietest: every rule involved is
#: authorised and unmodified, so nothing looks wrong on inspection, while the
#: EFFECTIVE policy has changed — a permissive rule moved above a restrictive
#: one passes traffic the restrictive one was written to stop.
CLASSES = ("malformed", "reordered", "unmanaged", "orphaned")

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str) -> str:
    return _UNSAFE_IN_FILENAME.sub("_", str(value)).strip("._-") or "object"


def violation_id(*, scope: str, name: str, first_seen: str) -> str:
    """A stable, readable handle for one finding: `VIOL-2026-0816-GitOps-web-1`.

    DERIVED, NOT ALLOCATED. A sequential counter would read better in a meeting,
    but it needs a source of truth to increment against, and two overlapping runs
    (a schedule firing during a dispatch) can both mint the same number and land
    conflicting records. Deriving it from what the finding already IS removes the
    allocator, and the id is therefore identical every run without coordination.

    NOT A HASH, deliberately — the point is that a human reading
    `Remediate unauthorised change — VIOL-2026-0816-GitOps-test-unmanaged-2` can
    see what it refers to without looking it up.

    THE SCOPE IS PART OF IT because the scope is part of the IDENTITY. The same
    object name in two folders is two different findings, and giving them one id
    would silently merge them in the join this exists to make possible.

    Stable across a resolve-and-return cycle: `first_seen` is never overwritten,
    so a finding that comes back keeps the id it was first known by.
    """
    # `.replace("-", "", 1)` drops the FIRST hyphen and yields 202608-16.
    # Split on the parts instead of trimming characters off a string.
    y, m, d = str(first_seen)[:10].split("-")           # 2026-08-16 -> 2026-0816
    day = f"{y}-{m}{d}"
    return f"VIOL-{day}-{_safe(scope)}-{_safe(name)}"


def record_path(root: Path, *, scope: str, kind: str, name: str) -> Path:
    """Stable path per VIOLATION IDENTITY (scope + kind + name).

    Stable is the point: the same violation seen on ten nights updates one file
    rather than creating ten. The name is sanitised — SCM names may contain
    slashes, and a record written into a directory that does not exist is a
    record nobody will find.
    """
    return Path(root) / f"{_safe(scope)}-{_safe(kind)}-{_safe(name)}.json"


def build(*, cls: str, kind: str, scope: str, name: str,
          tags: Sequence[str], run_url: str, at: str) -> Dict[str, Any]:
    if cls not in CLASSES:
        raise ValueError(f"unknown violation class {cls!r}; expected one of {CLASSES}")
    return {
        "schema": SCHEMA,
        "id": violation_id(scope=scope, name=name, first_seen=at),
        "class": cls,
        "kind": kind,
        "scope": scope,
        "name": name,
        "tags_observed": list(tags),
        "status": "open",
        "first_seen": at,
        "last_seen": at,
        "first_seen_run": run_url,
        "last_seen_run": run_url,
        "resolved_at": None,
    }


def reconcile(*, found: Iterable[Dict[str, Any]], existing: Dict[Path, Dict[str, Any]],
              root: Path, run_url: str, at: Optional[str] = None,
              scopes_checked: Sequence[str] = ()) -> List[Tuple[Path, Dict[str, Any]]]:
    """Merge this run's findings with the records already on disk.

    `found` is `{cls, kind, scope, name, tags}` per current violation.
    Returns `(path, record)` for every record that CHANGED — so a run where
    nothing moved writes nothing and opens no pull request.

    RESOLUTION IS SCOPED. A record is only closed if its scope was actually
    CHECKED this run: a folder that failed to read, or was skipped, must not
    silently resolve every violation in it. That is the difference between "we
    looked and it is gone" and "we did not look".
    """
    at = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    changed: List[Tuple[Path, Dict[str, Any]]] = []
    seen_paths = set()

    for f in found:
        p = record_path(root, scope=f["scope"], kind=f["kind"], name=f["name"])
        seen_paths.add(p)
        prior = existing.get(p)
        if prior is None:
            changed.append((p, build(cls=f["cls"], kind=f["kind"], scope=f["scope"],
                                     name=f["name"], tags=f.get("tags", []),
                                     run_url=run_url, at=at)))
            continue
        rec = dict(prior)
        # REOPENED. A violation that comes back is the same finding returning,
        # not a new one — the history of when it first appeared is the useful
        # part, so first_seen is never overwritten.
        rec["status"] = "open"
        rec["resolved_at"] = None
        rec["last_seen"] = at
        rec["last_seen_run"] = run_url
        rec["tags_observed"] = list(f.get("tags", []))
        rec["class"] = f["cls"]
        # A NIGHT WHERE NOTHING MOVED MUST WRITE NOTHING.
        #
        # `last_seen` alone advances on EVERY run — it is `now()` — so treating
        # it as a change meant the same three violations reopened a pull request
        # every night, saying nothing new. That is how the pull requests that DO
        # matter stop being read. Only a real transition writes: a new finding,
        # a reopening, a class change, or different tags observed.
        #
        # The consequence, deliberately: `last_seen` is "when this record last
        # CHANGED", not "when we last looked". Whether a finding is still open
        # is answered by `resolved_at` being null, never by the age of
        # `last_seen`.
        if _material(prior, rec):
            changed.append((p, rec))

    for p, rec in existing.items():
        if p in seen_paths or rec.get("status") != "open":
            continue
        if rec.get("scope") not in scopes_checked:
            # NOT LOOKED AT — NOT RESOLVED, and an EMPTY set means nothing was
            # checked, never "everything was". `fwgitops snapshot` writes a bare
            # list of rows, so a folder whose read failed or returned nothing
            # yields no scopes at all — under the old `if scopes_checked and ...`
            # that skipped the guard entirely and CLOSED EVERY OPEN VIOLATION.
            # An outage in the checker would have read as a clean bill of health,
            # which is the single failure this module exists to prevent.
            continue
        closed = dict(rec)
        closed["status"] = "resolved"
        closed["resolved_at"] = at
        changed.append((p, closed))

    return changed


_TOUCH_ONLY = ("last_seen", "last_seen_run")


def _material(prior: Dict[str, Any], rec: Dict[str, Any]) -> bool:
    """Did anything change beyond "we looked again and it is still there"?"""
    return any(prior.get(k) != rec.get(k)
               for k in set(prior) | set(rec) if k not in _TOUCH_ONLY)


def load(root: Path) -> Dict[Path, Dict[str, Any]]:
    out: Dict[Path, Dict[str, Any]] = {}
    if not Path(root).is_dir():
        return out
    for p in sorted(Path(root).glob("*.json")):
        try:
            out[p] = json.loads(p.read_text())
        except (OSError, ValueError):
            continue          # a malformed record is not a reason to lose the run
    return out


def write(changed: Iterable[Tuple[Path, Dict[str, Any]]]) -> List[Path]:
    written = []
    for p, rec in changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        _assert_id_is_unique(p, rec)
        p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(p)
    return written


def _assert_id_is_unique(path: Path, rec: Dict[str, Any]) -> None:
    """One file, one finding — and one id, one finding.

    Both record FILENAMES and ids are built from sanitised text, so `web/1` and
    `web 1` collapse to the same `web_1`. Two different SCM objects then share
    one record file, and the second silently OVERWRITES the first: a finding
    disappears with nothing to show it ever existed. That predates ids —
    `record_path` has always sanitised — and was invisible precisely because the
    result is one file where there should be two.

    Checked in both directions here: this path must not already hold a DIFFERENT
    identity, and no other file may already claim this id.
    """
    rid = rec.get("id")
    identity = (rec.get("scope"), rec.get("kind"), rec.get("name"))
    if path.exists():
        try:
            prior = json.loads(path.read_text())
        except (OSError, ValueError):
            prior = None
        if prior is not None:
            prior_identity = (prior.get("scope"), prior.get("kind"), prior.get("name"))
            if prior_identity != identity:
                raise ValueError(
                    f"{path.name} already holds the finding for "
                    f"{prior_identity[0]}/{prior_identity[2]!r}, and "
                    f"{identity[0]}/{identity[2]!r} sanitises to the same "
                    f"filename. Writing would erase a finding — two objects "
                    f"whose names differ only in characters this strips need "
                    f"two records, not one.")
    if not rid:
        return
    for other in sorted(path.parent.glob("*.json")):
        if other == path:
            continue
        try:
            existing = json.loads(other.read_text())
        except (OSError, ValueError):
            continue
        if existing.get("id") == rid:
            raise ValueError(
                f"violation id {rid!r} is already used by {other.name} for "
                f"{existing.get('scope')}/{existing.get('name')}, and would now "
                f"also mean {rec.get('scope')}/{rec.get('name')}. Two findings "
                f"cannot share one id — a remediation record pointing at it "
                f"would not say which change it removed.")


def summarise(records: Iterable[Dict[str, Any]]) -> str:
    """One line per open violation, worst class first — for the run summary."""
    open_ = [r for r in records if r.get("status") == "open"]
    if not open_:
        return "no open violations"
    lines = [f"{len(open_)} open violation(s):"]
    for cls in CLASSES:
        for r in sorted((x for x in open_ if x.get("class") == cls),
                        key=lambda x: (x.get("scope", ""), x.get("name", ""))):
            lines.append(f"  {cls:10} {r.get('scope')}/{r.get('name')}"
                         f"  first seen {r.get('first_seen')}")
    return "\n".join(lines)
