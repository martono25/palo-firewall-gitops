"""Tag objects are CREATED by this platform and never destroyed by Terraform.

WHY (ADR-0009, measured in `spike/tag-destroy-ordering` on 2026-08-10). Changing
a tag VALUE on a live rule failed the apply: Terraform ran the tag DESTROY before
the rule UPDATE that released it, and SCM refused with `409 NON_ZERO_REFS`. Once
the rule's config no longer references the old tag, nothing orders the destroy
after the update — the edge that existed for creation is gone exactly when it is
needed for destruction.

So the two halves are separated in time:

    ensure_tags   before apply — create what is missing, touch nothing else
    <terraform apply + push>
    sweep_tags    after push   — remove gitops: tags nothing references

TWO RULES, both fail-safe:

  * **Only `gitops:` tags are swept.** A tag this platform did not create is
    never deleted, whatever references it has. Somebody else's tag is not ours to
    tidy.
  * **A tag is swept only when NOTHING references it.** References are read from
    SCM, not inferred from the intent tree — an object created outside GitOps can
    reference a `gitops:` tag, and deleting it would break their config to tidy
    ours. If the reference read fails, sweep nothing: an unreferenced tag is
    inert, and deleting a referenced one is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from fwgitops.tags import NAMESPACE, SEP

#: Only tags in this namespace are ever created or removed here.
GITOPS_PREFIX = f"{NAMESPACE}{SEP}"

TAGS_PATH = "/config/objects/v1/tags"
#: Everything that can carry a tag and therefore hold a reference. Read from SCM
#: rather than derived from the intent tree: an object created outside GitOps can
#: reference a `gitops:` tag, and sweeping it would break their config to tidy
#: ours.
REFERRER_PATHS = (
    "/config/security/v1/security-rules",
    "/config/objects/v1/addresses",
    "/config/objects/v1/services",
    "/config/objects/v1/address-groups",
    "/config/objects/v1/service-groups",
)


@dataclass(frozen=True)
class TagPlan:
    """What ensure/sweep would do, so a caller can report before acting."""

    missing: List[str] = field(default_factory=list)
    unreferenced: List[str] = field(default_factory=list)
    #: `gitops:` tags still referenced — reported, never touched.
    referenced: List[str] = field(default_factory=list)
    #: Non-`gitops:` tags. Counted only, so "we left N alone" is sayable.
    foreign: int = 0


def _rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    data = (payload or {}).get("data")
    return [r for r in (data or []) if isinstance(r, dict)]


def existing_tags(session: Any, scope_params: Dict[str, str]) -> Dict[str, str]:
    """`name -> id` for every tag at this scope, gitops or not."""
    payload = session.request("GET", TAGS_PATH, params={**scope_params, "limit": 500})
    return {str(r["name"]): str(r.get("id", "")) for r in _rows(payload) if r.get("name")}


def referenced_tags(session: Any, scope_params: Dict[str, str]) -> Set[str]:
    """Every tag name referenced by any object SCM reports at this scope.

    Raises whatever the session raises. The caller must treat a failure as
    "sweep nothing": a partial reference set would make a referenced tag look
    unreferenced, and deleting one of those is the 409 this whole design exists
    to avoid — except done deliberately, which is worse.
    """
    used: Set[str] = set()
    for path in REFERRER_PATHS:
        payload = session.request("GET", path, params={**scope_params, "limit": 500})
        for row in _rows(payload):
            for t in (row.get("tag") or row.get("tags") or []):
                if isinstance(t, str):
                    used.add(t)
    return used


def plan_tags(wanted: Iterable[str], present: Dict[str, str],
              used: Set[str]) -> TagPlan:
    """Pure: what to create, what is safe to remove, what to leave alone."""
    wanted = sorted({w for w in wanted if w.startswith(GITOPS_PREFIX)})
    ours = sorted(n for n in present if n.startswith(GITOPS_PREFIX))
    return TagPlan(
        missing=[w for w in wanted if w not in present],
        # Wanted OR referenced keeps it. `wanted` matters because a tag can be
        # created by ensure_tags and swept before the apply that references it
        # has run — the window between the two steps.
        unreferenced=[n for n in ours if n not in used and n not in set(wanted)],
        referenced=[n for n in ours if n in used],
        foreign=sum(1 for n in present if not n.startswith(GITOPS_PREFIX)),
    )


def ensure_tags(session: Any, scope_params: Dict[str, str],
                wanted: Sequence[str], *, dry_run: bool = False) -> TagPlan:
    """Create missing `gitops:` tags. Idempotent; never deletes."""
    present = existing_tags(session, scope_params)
    plan = plan_tags(wanted, present, used=set())
    if dry_run:
        return plan
    for name in plan.missing:
        session.request("POST", TAGS_PATH,
                        body={**scope_params, "name": name,
                              "comments": "Managed by fwgitops"})
    return plan


def sweep_tags(session: Any, scope_params: Dict[str, str],
               wanted: Sequence[str], *, dry_run: bool = False) -> TagPlan:
    """Delete `gitops:` tags nothing references. Never touches a foreign tag."""
    present = existing_tags(session, scope_params)
    used = referenced_tags(session, scope_params)
    plan = plan_tags(wanted, present, used)
    if dry_run:
        return plan
    for name in plan.unreferenced:
        tag_id = present.get(name)
        if tag_id:
            session.request("DELETE", f"{TAGS_PATH}/{tag_id}")
    return plan
