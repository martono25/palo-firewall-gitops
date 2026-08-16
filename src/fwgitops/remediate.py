"""Delete what nobody authorised, automatically.

THE POLICY, decided 2026-08-16: an unmanaged object or rule is REMEDIATED BY THE
PIPELINE, not by a human dispatching a workflow. GitOps is the source of truth,
so config that no request authorised does not survive to the next day.

THE OBJECTION, and its answer. Emergency fixes happen — someone opens a path at
2am to restore a service. Automatic deletion means that fix is destroyed by the
next remediation run. That is accepted deliberately: it is a TIMING constraint,
not a reason to keep deletion manual. The admin must raise and apply a normal
AccessRequest before the run. The window is therefore a published operational
fact, not an implementation detail, and it belongs in the runbook next to the
emergency procedure.

Note what "convert it" means, because it is not adoption (ADR-0011): the
AccessRequest creates a NEW rule under its request id. The hand-made one stays
unmanaged and IS deleted. That is the intended end state — the permanent rule
exists, the temporary one does not.

WHAT IS REMOVED. Every drift class is unauthorised state, so the question is not
which classes count — it is what the correct remediation IS for each. One rule
decides it, and it comes straight from source-of-truth:

    DOES GIT DECLARE THIS OBJECT?

  * `unmanaged` — not declared. DELETE.
  * `orphaned` — not declared; that is the definition of the class. DELETE.
  * `malformed` whose name matches no declared request — a console COPY of a
    managed rule, inheriting its tags. Not declared under its own name. DELETE.
  * `malformed` whose name IS a declared request — Git says this rule should
    exist and its tags are damaged. REPAIRED BY APPLY, which rewrites them.
    Deleting an authorised rule and waiting for the next apply to recreate it is
    an outage that a labelling defect does not warrant.

An earlier version deleted `unmanaged` only, on the reasoning that the other two
"need judgement". They do not: what needed deciding was the ACTION, not whether
the state was authorised. Config Git does not declare is removed; config Git
declares is restored.

And never, in any class: anything SCM provides (`snippet`), anything inherited
from an ancestor folder, anything carrying a `gitops:req` tag naming a request
that IS declared, or anything whose id SCM did not report.

IT RE-DETECTS RATHER THAN READING THE RECORDS. Acting on a violation record
written hours earlier would delete based on a stale observation — the object may
since have been replaced by an authorised one, or the record may describe a
scope this run could not read. Remediation acts on what it sees NOW, and the
record it writes afterwards says what it actually removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fwgitops.objectdrift import ClassifiedObject
from fwgitops.objectsweep import KIND_PATHS

#: A rule tagged with a request belongs to an intent. Removing it is the
#: deletion contract's job (ADR-0008), which produces evidence and an approval;
#: deleting it here would strip a live rule and leave Terraform's state pointing
#: at nothing.
OWNED_TAG_PREFIX = "gitops:req"


@dataclass(frozen=True)
class Removal:
    kind: str                 # address | service | security-rule
    name: str
    scope: str
    object_id: str
    tags: Sequence[str]


def _refuse_reasons(*, name: str, tags: Sequence[str], object_id: Optional[str],
                    provenance: str, declared: Sequence[str] = ()) -> List[str]:
    """Every reason this must NOT be deleted. Empty means it may be.

    `declared` is the set of request ids Git declares for this scope. An object
    NAMED after one of them is config Git says should exist — whatever is wrong
    with it, the remedy is to repair it, not to remove it.
    """
    bad: List[str] = []
    if provenance not in ("unmanaged", "orphaned", "malformed"):
        bad.append(f"provenance is {provenance!r} — not a drift class")
    # THE OBJECT'S OWN NAME, never a tag it carries.
    #
    # Keying on the tag looked like belt and braces and broke the case this
    # exists for: a console COPY of a managed rule inherits
    # `gitops:req:REQ-2026-0725`, so a tag-based guard protected the forgery
    # precisely because it was wearing the original's label. A managed rule is
    # NAMED after its request (`name = metadata.id`), so the name is the claim
    # that cannot be inherited — which is the same reasoning that made the
    # duplicate detectable at all.
    if name in set(declared):
        bad.append(f"{name!r} is declared in Git; apply repairs it, deletion "
                   f"would remove authorised config")
    if not object_id:
        bad.append("SCM reported no id, so the delete would be a guess")
    if not name:
        bad.append("no name")
    return bad


def removals_for_objects(objects: Iterable[ClassifiedObject],
                         ids: Dict[str, str],
                         declared: Sequence[str] = ()) -> List[Removal]:
    """The subset of a folder's objects that may be deleted automatically.

    `ids` maps name -> SCM id for the same scope. An object whose id is unknown
    is skipped rather than looked up by name: a delete addressed by anything but
    the id SCM returned for the row we classified is a different object.
    """
    out: List[Removal] = []
    for o in objects:
        if _refuse_reasons(name=o.name, tags=o.tags, object_id=ids.get(o.name),
                           provenance=o.provenance, declared=declared):
            continue
        out.append(Removal(kind=o.kind, name=o.name, scope=o.scope,
                           object_id=ids[o.name], tags=tuple(o.tags)))
    return out


def removals_for_rules(rows: Iterable[Dict[str, Any]], *, scope: str,
                       drifted_names: Sequence[str],
                       declared: Sequence[str] = ()) -> List[Removal]:
    """Unmanaged security rules, from the same snapshot drift classified.

    `drifted_names` comes from the tag engine — unmanaged, orphaned, and the
    malformed rules that trace to no declared request. The rows are re-read here
    solely for the id and the tags, and every guard is applied again: a caller
    passing the wrong list must not be able to cause a deletion.
    """
    want = set(drifted_names)
    out: List[Removal] = []
    for r in rows:
        if not isinstance(r, dict) or str(r.get("name", "")) not in want:
            continue
        tags = [t for t in (r.get("tag") or r.get("tags") or []) if isinstance(t, str)]
        # DEFINED HERE, not inherited: a folder read returns ancestors' rules,
        # and deleting one of those from a child scope is somebody else's config.
        if r.get("folder") not in (None, scope):
            continue
        if _refuse_reasons(name=str(r.get("name", "")), tags=tags,
                           object_id=str(r.get("id") or ""),
                           provenance="unmanaged", declared=declared):
            continue
        out.append(Removal(kind="security-rule", name=str(r["name"]), scope=scope,
                           object_id=str(r["id"]), tags=tuple(tags)))
    return out


RULE_PATH = "/config/security/v1/security-rules"


def path_for(kind: str) -> str:
    return RULE_PATH if kind == "security-rule" else KIND_PATHS[kind]
