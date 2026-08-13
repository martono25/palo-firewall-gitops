"""Address and service objects are CREATED before apply and never destroyed by
Terraform — the lifecycle ADR-0009 gave tags, extended to the objects that have
the same relationship to a rule.

WHY (ADR-0010, measured 2026-08-13 on the pilot). Widening a live rule's
destination failed the apply. Terraform planned to update the rule in place and
destroy the address object the old value no longer needed, ran the DESTROY
first, and SCM refused:

    Error deleting addresses / 409 Conflict
    errorType: Reference Not Zero
    Node cannot be deleted because of references from params:[addr-a102bfc799]
    container/[prod-edge]/pre-rulebase/security/rules/[REQ-2026-0809]/destination

The rule still pointed at the object because its update had not run. This is the
same failure ADR-0009 recorded for tags, for the same reason: once the rule's
config no longer references the old object, nothing orders the destroy after the
update. It is a documented Terraform limitation, not a provider defect —
hashicorp/terraform#32136 records that update-before-destroy ordering is only
guaranteed when the child is being RECREATED under `create_before_destroy`, and
a pure delete has no such guarantee.

It was not caught earlier because it needs an object referenced by exactly one
rule. Most are: on the pilot folder, six of eight addresses and one service.

So the two halves are separated in time:

    ensure_objects   before apply — create what is missing, touch nothing else
    <terraform apply + push>
    sweep_objects    after push   — remove objects nothing references

TWO RULES, both fail-safe, and both stronger here than for tags:

  * **Only objects this platform MINTED are swept**, and that is PROVEN rather
    than assumed. Object names are content-addressed — `addr-` plus the first
    ten hex of sha256(value) — so an object is ours exactly when its name equals
    the name its own value hashes to. A tag can only be recognised by a name
    prefix, which a foreigner can imitate; a hash collision cannot be typed by
    hand.

  * **An object is swept only when NOTHING references it**, and references are
    read from SCM rather than inferred from the intent tree, because an object
    created outside GitOps can reference ours. References are found by walking
    every referring object's JSON for the name ANYWHERE in it, not by reading
    named fields. Over-detecting a reference costs a leftover object, which is
    inert. Under-detecting one deletes something a rule is using, which is the
    409 this design exists to avoid — done deliberately, which is worse. Field
    names are a guess; a substring walk is not.

If the reference read fails, sweep nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fwgitops.tags import object_name

#: kind -> the collection that holds it.
KIND_PATHS: Dict[str, str] = {
    "address": "/config/objects/v1/addresses",
    "service": "/config/objects/v1/services",
}

#: What can hold a reference to an object, PER KIND. Not one shared set:
#:
#: The tag sweep reads `/config/objects/v1/addresses` because an ADDRESS carries
#: tags. Reusing that set for addresses is wrong in a way that looks harmless —
#: the addresses collection contains the address objects themselves, so every
#: address's own `name` turns up in the walk, every address looks referenced,
#: and NOTHING is ever swept. Silent, permanent, and indistinguishable from a
#: tenant that simply has no garbage.
#:
#: An address can only be referenced by a rule or an address group; a service by
#: a rule or a service group.
#:
#: Every path here is proven: the tag sweep reads them on every apply. The first
#: live run (2026-08-13) also carried `/config/nat/v1/nat-rules`, invented from
#: the shape of the others — SCM returned 404, the read raised, and the sweep
#: ran on nothing. Fail-closed worked; the guess did not.
#:
#: NAT rules are included since 2026-08-13, on a path CONFIRMED against the live
#: tenant rather than inferred. The probe (`.github/workflows/probe-scm-path.yml`)
#: answered:
#:
#:     /config/nat/v1/nat-rules        HTTP 404   <- the original inference
#:     /config/network/v1/nat-rules    200 OK     <- the real one
#:     /config/objects/v1/nat-rules    HTTP 403
#:     /config/security/v1/nat-rules   HTTP 403
#:
#: This platform has no NatRequest kind and never writes a NAT rule, so the
#: exposure was never anything GitOps produces. It was a NAT rule created by
#: hand in SCM pointed at one of our `addr-<hash>` objects — and prod-edge does
#: contain a NAT rule, so the gap was real rather than hypothetical. It
#: references none of our objects today, checked before this path was added.
REFERRER_PATHS: Dict[str, Tuple[str, ...]] = {
    "address": (
        "/config/security/v1/security-rules",
        "/config/objects/v1/address-groups",
        "/config/network/v1/nat-rules",
    ),
    "service": (
        "/config/security/v1/security-rules",
        "/config/objects/v1/service-groups",
        "/config/network/v1/nat-rules",
    ),
}


class ReferenceReadError(RuntimeError):
    """A referrer collection could not be read, so the sweep must not run.

    Its own type so a caller can tell "I could not determine what is in use"
    apart from "the delete failed" — the first is a reason to do nothing, the
    second is a reason to report.
    """


@dataclass(frozen=True)
class ObjectPlan:
    """What ensure/sweep would do, so a caller can report before acting."""

    kind: str = "address"
    missing: List[str] = field(default_factory=list)
    unreferenced: List[str] = field(default_factory=list)
    #: Ours, still referenced — reported, never touched.
    referenced: List[str] = field(default_factory=list)
    #: Objects at this scope this platform did not mint. Counted only, so
    #: "we left N alone" is sayable in the run log.
    foreign: int = 0


def _rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    data = (payload or {}).get("data")
    return [r for r in (data or []) if isinstance(r, dict)]


def is_ours(kind: str, name: str, value: Optional[str]) -> bool:
    """Did this platform mint `name`?

    True only when the name is exactly the name its own value hashes to. An
    object whose value SCM does not report cannot be proven ours, so it is not
    ours — the fail-safe direction, since the consequence is leaving it alone.
    """
    if not value:
        return False
    try:
        return object_name(kind, value) == name
    except ValueError:
        return False


def _value_of(row: dict) -> Optional[str]:
    """The value an object is named after.

    SCM reports an address's value under its TYPE (`ip_netmask`, `fqdn`, …) and
    a service's under its protocol. The compiler names both from the same string
    it sent, so any of these that round-trips is the one that hashes.
    """
    for key in ("ip_netmask", "ip_range", "ip_wildcard", "fqdn", "value"):
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


def existing_objects(session: Any, kind: str,
                     scope_params: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """`name -> {id, value}` for every object of `kind` at this scope."""
    payload = session.request("GET", KIND_PATHS[kind],
                              params={**scope_params, "limit": 500})
    out: Dict[str, Dict[str, Any]] = {}
    for r in _rows(payload):
        name = r.get("name")
        if name:
            out[str(name)] = {"id": str(r.get("id", "")), "value": _value_of(r)}
    return out


def _strings(node: Any) -> Iterable[str]:
    """Every string anywhere in a JSON document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def referenced_names(session: Any, kind: str, scope_params: Dict[str, str]) -> Set[str]:
    """Every string appearing anywhere in anything that could refer to `kind`.

    Deliberately blunt. See the module docstring: over-detection leaves an inert
    object behind, under-detection deletes one in use.

    Raises whatever the session raises. A caller MUST treat failure as "sweep
    nothing".
    """
    used: Set[str] = set()
    for path in REFERRER_PATHS[kind]:
        try:
            payload = session.request("GET", path, params={**scope_params, "limit": 500})
        except Exception as e:
            # STILL FATAL — a partial reference set is what makes a referenced
            # object look unreferenced. But it must say WHICH path and WHY it
            # matters. The first live failure printed `SCM API error 404: {}`
            # and nothing else; the cause (a path invented from the shape of its
            # neighbours) was found by remembering having written it, which does
            # not generalise to whoever meets this next.
            raise ReferenceReadError(
                f"could not read {path!r} while checking which {kind} objects are "
                f"still in use ({e}). Sweeping nothing: an incomplete reference "
                f"set makes a referenced object look unreferenced, and deleting "
                f"one of those is the 409 this design exists to avoid. A 404 here "
                f"means the path does not exist at this scope — check it against "
                f"the SCM API reference rather than inferring it from the others."
            ) from e
        for row in _rows(payload):
            used.update(_strings(row))
    return used


def plan_objects(kind: str, wanted: Iterable[str],
                 present: Dict[str, Dict[str, Any]],
                 used: Set[str]) -> ObjectPlan:
    """Pure: what to create, what is safe to remove, what to leave alone."""
    wanted_set = {w for w in wanted}
    ours = sorted(n for n, meta in present.items()
                  if is_ours(kind, n, meta.get("value")))
    return ObjectPlan(
        kind=kind,
        missing=sorted(w for w in wanted_set if w not in present),
        # Wanted OR referenced keeps it. `wanted` matters because an object can
        # be created by ensure_objects and swept before the apply that
        # references it has run — the window between the two steps.
        unreferenced=[n for n in ours if n not in used and n not in wanted_set],
        referenced=[n for n in ours if n in used],
        foreign=sum(1 for n, meta in present.items()
                    if not is_ours(kind, n, meta.get("value"))),
    )


def ensure_objects(session: Any, kind: str, scope_params: Dict[str, str],
                   wanted: Dict[str, Dict[str, Any]], *,
                   dry_run: bool = False) -> ObjectPlan:
    """Create missing objects. Idempotent; never deletes, never edits.

    `wanted` maps name -> the body fields that define it (`ip_netmask`,
    `protocol`, …), which the compiler already produces. Never editing is what
    makes this safe to run before every apply: an object's value defines its
    name, so an object that exists under the right name already holds the right
    value.
    """
    present = existing_objects(session, kind, scope_params)
    plan = plan_objects(kind, wanted.keys(), present, used=set())
    if dry_run:
        return plan
    for name in plan.missing:
        session.request("POST", KIND_PATHS[kind],
                        body={**scope_params, "name": name, **wanted[name]})
    return plan


def sweep_objects(session: Any, kind: str, scope_params: Dict[str, str],
                  wanted: Iterable[str], *, dry_run: bool = False) -> ObjectPlan:
    """Delete objects this platform minted that nothing references."""
    present = existing_objects(session, kind, scope_params)
    used = referenced_names(session, kind, scope_params)
    plan = plan_objects(kind, wanted, present, used)
    if dry_run:
        return plan
    for name in plan.unreferenced:
        obj_id = present.get(name, {}).get("id")
        if obj_id:
            session.request("DELETE", f"{KIND_PATHS[kind]}/{obj_id}")
    return plan
