"""Map an address, a name or a ticket back to the intent that authorised it.

THE QUESTION THIS ANSWERS. An incident responder has a firewall log line and one
hour. They know a source IP, a destination IP, and maybe a rule name. What they
need is: *which request permitted this, who asked for it, under what ticket, and
is it still supposed to exist?* Every field for that answer is already in this
repository — spread across `intent/`, the compiled desired-state and
`evidence/` — and nothing joined them up.

WHY `grep` IS NOT THIS. The log says `10.20.9.10`. The intent says
`10.20.9.0/24`. Grep returns nothing, and returning nothing is the worst possible
answer here because it is indistinguishable from "no rule permits this" — which
is the conclusion someone will draw at 3am. So the match is by CONTAINMENT:

    fwgitops where 10.20.9.10
      -> REQ-2026-0727  destination 10.20.9.0/24 contains it

The same reasoning applies to routes. "Which rule allowed it" and "which route
carried it" are different questions with the same input, and ADR-0008 established
that a route is the one whose removal is a silent outage — so a query about an
address reports both, and marks which route is EFFECTIVE by longest prefix.

WHY IT SEARCHES THE COMPILED STATE. An intent may name an app (`app: payments`)
whose addresses live in the catalog; the raw YAML never contains the CIDR at all.
Compiling resolves that, so the search runs over compiled objects and reports the
intent that produced them. Searching the YAML would miss precisely the indirection
the catalog exists to provide.

WHY THE WALK IS GENERIC. Every compiled kind is a dataclass, so the searchable
surface is `asdict()` — which means a NEW KIND IS SEARCHABLE THE DAY IT IS
REGISTERED, with no matcher to remember. The alternative, a per-kind list of
"fields worth searching", is the shape that let `ZoneRequest` ship wired into
three stages and missing from four (ADR-0004).
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

Net = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

#: Field paths whose values are addresses that CARRY traffic rather than match
#: it. Used only to label a hit, never to decide one.
_ROUTE_DESTINATION = "destination"


@dataclass(frozen=True)
class Query:
    """What the user asked about, once interpreted.

    `network` is set when the text is an IP or CIDR. An IP becomes a /32 so the
    containment test is one code path rather than two.
    """

    text: str
    network: Optional[Net] = None

    @property
    def is_address(self) -> bool:
        return self.network is not None

    @property
    def is_host(self) -> bool:
        return self.network is not None and self.network.num_addresses == 1

    @classmethod
    def parse(cls, text: str) -> "Query":
        text = text.strip()
        try:
            return cls(text=text, network=ipaddress.ip_network(text, strict=False))
        except ValueError:
            return cls(text=text)


@dataclass(frozen=True)
class Hit:
    """One reason a compiled object answers the query."""

    kind: str
    req_id: str
    scope: str
    #: Dotted path into the compiled object, e.g. `rule.destinations[1]`.
    field: str
    value: str
    #: Plain English. A hit whose reason is not obvious from `value` — every
    #: containment match — is useless without it: `10.20.9.0/24` next to a query
    #: for `10.20.9.10` reads as a near miss unless something says it contains it.
    why: str
    #: True when this hit is the ROUTE that would actually carry the address
    #: (longest prefix in its VRF). A default route matches everything, so a list
    #: of matching routes without this is noise.
    effective_route: bool = False


def _as_network(value: str) -> Optional[Net]:
    """A CIDR or bare IP, or None. Never raises — most strings are not addresses."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def walk(obj: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
    """(dotted_path, scalar) for every leaf of a compiled dataclass.

    Lists keep their index, because "which of the three destinations matched"
    is the difference between a useful answer and a shrug.
    """
    if is_dataclass(obj):
        obj = asdict(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from walk(v, f"{prefix}[{i}]")
    elif obj is not None:
        yield prefix, obj


def match_value(query: Query, path: str, value: Any) -> Optional[str]:
    """Why `value` answers `query`, or None.

    Address queries test CONTAINMENT BOTH WAYS. A responder may hold a host from
    a log (inside the intent's /24) or a subnet from a change request (containing
    the intent's /32); reporting only the first would answer half the questions
    asked and look complete doing it.
    """
    if not isinstance(value, str):
        return None

    if query.is_address:
        net = _as_network(value)
        if net is None or net.version != query.network.version:
            return None
        if net.subnet_of(query.network) and net != query.network:
            return f"{value} is inside the queried range {query.text}"
        if query.network.subnet_of(net) and net != query.network:
            return f"{value} contains {query.text}"
        if net == query.network:
            return f"{value} is exactly {query.text}"
        return None

    # Non-address: exact, case-insensitive. NOT a substring match — `dmz` must not
    # hit `dmz-legacy`, because a responder acting on the wrong zone is worse off
    # than one who got no answer.
    if value.lower() == query.text.lower():
        return f"{path} is exactly {query.text!r}"
    return None


def find(query: Query, items: Iterable[Tuple[str, str, str, Any]]) -> List[Hit]:
    """Search compiled objects. `items` is (kind, req_id, scope, compiled).

    Returns every reason, not the first: a rule matching on BOTH source and
    destination is a rule permitting traffic from a host to itself, which is
    worth seeing rather than collapsing.
    """
    hits: List[Hit] = []
    for kind, req_id, scope, compiled in items:
        for path, value in walk(compiled):
            why = match_value(query, path, value)
            if why:
                hits.append(Hit(kind=kind, req_id=req_id, scope=scope,
                                field=path, value=str(value), why=why))
    return mark_effective_routes(hits, query)


def mark_effective_routes(hits: List[Hit], query: Query) -> List[Hit]:
    """Flag the route that would actually carry the address.

    A default route matches every address, so "3 routes match" is noise without
    saying which one wins. Longest prefix per SCOPE — routes in different scopes
    are on different firewalls and do not compete.

    Only for a host query. Asking about a /16 has no single effective route, and
    inventing one would be a confident wrong answer of the kind this command is
    supposed to prevent.
    """
    if not query.is_host:
        return hits
    best: Dict[str, Tuple[int, int]] = {}      # scope -> (prefixlen, hit index)
    for i, h in enumerate(hits):
        if h.kind != "RouteRequest" or not h.field.endswith(_ROUTE_DESTINATION):
            continue
        net = _as_network(h.value)
        if net is None:
            continue
        cur = best.get(h.scope)
        if cur is None or net.prefixlen > cur[0]:
            best[h.scope] = (net.prefixlen, i)
    winners = {i for _, i in best.values()}
    return [
        Hit(**{**h.__dict__, "effective_route": True,
               "why": f"{h.why} — LONGEST PREFIX in {h.scope}, so this is the route "
                      f"that carries it"})
        if i in winners else h
        for i, h in enumerate(hits)
    ]
