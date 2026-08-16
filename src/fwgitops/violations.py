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
  * `malformed` — an object carries the `gitops:managed` marker but no
    `gitops:req` tag. It CLAIMS this platform's provenance while tracing to no
    request, which is worse than an honest stranger: it would pass a check that
    only looked for the marker.
  * `orphaned` — a managed object still live in SCM whose request is gone from
    Git. Authorised once, no longer declared.

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
CLASSES = ("malformed", "unmanaged", "orphaned")

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str) -> str:
    return _UNSAFE_IN_FILENAME.sub("_", str(value)).strip("._-") or "object"


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
        if rec != prior:
            changed.append((p, rec))

    for p, rec in existing.items():
        if p in seen_paths or rec.get("status") != "open":
            continue
        if scopes_checked and rec.get("scope") not in scopes_checked:
            continue          # not looked at — not resolved
        closed = dict(rec)
        closed["status"] = "resolved"
        closed["resolved_at"] = at
        changed.append((p, closed))

    return changed


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
        p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(p)
    return written


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
