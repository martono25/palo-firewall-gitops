"""Address and service objects in a managed folder, classified by provenance.

WHY THIS EXISTS. The design premise is that GitOps is the source of truth: apart
from what SCM itself provides, everything in a managed folder came from here.
That premise held for rules, interfaces, routes and zones, and was false for
address and service objects — nothing looked at them at all. Proven on
2026-08-16: an address created straight into the GitOps folder sat live in SCM
while the nightly job reported "No drift of any kind".

The reason it was missed is worth keeping. `objectsweep.is_ours` returns true
only when a name is exactly the hash of its own value, and that is the right
policy for DELETING — refusing to destroy what we cannot prove we made. It was
then carried into DETECTION, where "not ours, not our business" contradicts the
premise: in a managed folder there is no legitimate third category. Deleting and
detecting need opposite defaults, and one function was doing both jobs.

NO ALLOWLIST, DELIBERATELY. The obvious design was a `baseline_objects` list in
the env map naming the PAN defaults, mirroring `baseline_zones`. It was rejected
for a reason that applies to any such list: a baseline a user can edit is a way
to LAUNDER an unauthorised object by adding its name to it. The control would
have been a bypass wearing the costume of a control.

SCM answers the question itself, so provenance is derived live and there is
nothing on disk to tamper with. Confirmed against the tenant (run 31934912556):

    Palo Alto Networks Sinkhole   folder=All      snippet=default
    service-http, service-https   folder=All      snippet=predefined-snippet
    addr-10.90.1.10_32-88ef17b7   folder=GitOps   (no snippet)

RESIDUAL GAP, stated rather than hidden: `snippet` means "this came from a
snippet rather than from this folder", and snippets are a construct a user can
also create. An object placed in a hand-made snippet attached to the folder
would read as SCM-provided here. Closing that needs snippet-level management,
which this repository does not have — so snippet contents remain unmanaged
surface, and that is a known limit of this check, not a claim it makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fwgitops.objectsweep import is_ours

#: What SCM reports the value under, per object type. Mirrors
#: `objectsweep._value_of` — an object's value is what its name must hash to.
_VALUE_KEYS = ("ip_netmask", "ip_range", "ip_wildcard", "fqdn", "value")


def _value_of(row: Dict[str, Any]) -> Optional[str]:
    for key in _VALUE_KEYS:
        v = row.get(key)
        if isinstance(v, str) and v:
            return v
    proto = row.get("protocol")
    if isinstance(proto, dict):
        for p in ("tcp", "udp"):
            spec = proto.get(p)
            if isinstance(spec, dict) and spec.get("port"):
                return f"{p}/{spec['port']}"
    return None


@dataclass(frozen=True)
class ClassifiedObject:
    name: str
    kind: str                   # "address" | "service"
    scope: str                  # the folder that was QUERIED
    provenance: str             # scm | inherited | ours | unmanaged
    folder: Optional[str]       # where SCM says it is defined
    snippet: Optional[str]
    tags: Sequence[str]

    @property
    def is_violation(self) -> bool:
        return self.provenance == "unmanaged"


@dataclass(frozen=True)
class ObjectDriftReport:
    scope: str
    objects: Sequence[ClassifiedObject]

    @property
    def unmanaged(self) -> List[ClassifiedObject]:
        return [o for o in self.objects if o.is_violation]

    @property
    def is_clean(self) -> bool:
        return not self.unmanaged

    def summary(self) -> str:
        if not self.objects:
            return f"{self.scope}: no address or service objects"
        counts: Dict[str, int] = {}
        for o in self.objects:
            counts[o.provenance] = counts.get(o.provenance, 0) + 1
        parts = ", ".join(f"{counts[k]} {k}" for k in sorted(counts))
        if self.is_clean:
            return f"{self.scope}: {parts} — nothing unaccounted for"
        lines = [f"{self.scope}: {parts} — UNMANAGED OBJECT(S):"]
        for o in sorted(self.unmanaged, key=lambda x: (x.kind, x.name)):
            lines.append(f"  unmanaged {o.kind:8} {o.name}")
        return "\n".join(lines)


def classify(rows: Iterable[Dict[str, Any]], *, scope: str,
             kind: str) -> List[ClassifiedObject]:
    """Sort a folder's objects into four provenances.

    ORDER MATTERS, and the order is the argument:

      1. `snippet` -> SCM PROVIDED IT. Checked first because a predefined object
         also reports a folder (`All`), and testing the folder first would call
         it inherited, which is a different and wrong story.
      2. a different folder -> INHERITED from an ancestor. Somebody else's
         config, seen here only because a folder read returns the tree above it.
      3. the name hashes to its own value -> OURS. The compiler minted it.
      4. anything else -> UNMANAGED. Defined in THIS folder, by nobody this
         platform can account for. That is the whole point of the check: with
         1-3 excluded there is no innocent explanation left.
    """
    out: List[ClassifiedObject] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        name = str(row["name"])
        snippet = row.get("snippet")
        folder = row.get("folder")
        tags = [t for t in (row.get("tag") or row.get("tags") or [])
                if isinstance(t, str)]

        if snippet:
            prov = "scm"
        elif folder is not None and str(folder) != scope:
            prov = "inherited"
        elif is_ours(kind, name, _value_of(row)):
            prov = "ours"
        else:
            prov = "unmanaged"

        out.append(ClassifiedObject(
            name=name, kind=kind, scope=scope, provenance=prov,
            folder=str(folder) if folder is not None else None,
            snippet=str(snippet) if snippet else None, tags=tuple(tags)))
    return out


def detect(per_kind: Dict[str, Iterable[Dict[str, Any]]], *,
           scope: str) -> ObjectDriftReport:
    """Classify every kind's rows for one folder."""
    objects: List[ClassifiedObject] = []
    for kind, rows in sorted(per_kind.items()):
        objects.extend(classify(rows, scope=scope, kind=kind))
    return ObjectDriftReport(scope=scope, objects=objects)
