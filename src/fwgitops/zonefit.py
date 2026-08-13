"""Does a rule's zone pair match where its addresses actually route?

WHY THIS EXISTS. A rule's zones come from its ENVIRONMENT, not from its
addresses — `prod` is always `local -> internet`. That is documented, and until
now the documented consequence was "if your traffic crosses a different pair,
the rule will not match". This module measures the "if".

Measured on the pilot 2026-08-13. The firewall's interfaces carry
`10.100.1.0/24`, `10.100.2.0/24` and `10.100.3.0/24`. Every declared rule uses
`10.20.x.x`, which is on NONE of them — so those addresses match only the
default route, egress `ethernet1/2`, whose zone is `internet`. Both endpoints
resolve to `internet` while the rules say `local -> internet`. They are not
"maybe wrong depending on topology"; they cannot match.

HOW A ZONE IS DECIDED, and why this mirrors it. PAN-OS does not look up an
address in a table of subnets. It resolves the route, finds the egress
interface, and takes that interface's zone. So the same is done here: longest
prefix match over connected subnets and static routes, then interface -> zone.
Anything else would agree with the firewall by luck.

REPORT ONLY. Nothing here changes a tier or blocks a change: switching every
existing rule's zones is a policy decision, and a check that silently rewrote
them would be a worse version of the bug it is reporting.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Egress:
    """Where traffic for a prefix leaves, and the zone that puts it in."""

    prefix: ipaddress.IPv4Network
    interface_role: str
    zone: Optional[str]
    #: "connected" or "static" — a connected route is ground truth; a static one
    #: is only as good as the nexthop resolving to a connected subnet.
    via: str


@dataclass(frozen=True)
class RoutingView:
    """Enough of a firewall's forwarding table to answer "which zone?".

    Built from intent, not from the device, so it answers what the REPOSITORY
    believes — which is the thing a pull request can be checked against.
    """

    egresses: Tuple[Egress, ...] = ()

    def zone_for(self, address: str) -> Optional[str]:
        """The zone traffic to `address` would leave by, or None if nothing
        matches. Longest prefix wins, exactly as the firewall does it."""
        try:
            net = ipaddress.ip_network(address, strict=False)
        except ValueError:
            return None
        best: Optional[Egress] = None
        for e in self.egresses:
            if not net.subnet_of(e.prefix):
                continue
            if best is None or e.prefix.prefixlen > best.prefix.prefixlen:
                best = e
        return best.zone if best else None

    @classmethod
    def build(cls, *, interfaces: Iterable[Tuple[str, List[str]]],
              routes: Iterable[Tuple[str, str]],
              zone_of_role: Dict[str, Optional[str]]) -> "RoutingView":
        """
        interfaces: (role, [cidr, ...]) — the addresses on each interface.
        routes:     (destination_cidr, nexthop_ip) — static routes.
        zone_of_role: role -> zone name, or None where unknown.

        A static route's egress is the interface whose CONNECTED subnet contains
        its nexthop, which is how the device resolves it too. A nexthop that
        matches no connected subnet yields no egress rather than a guess: an
        unresolvable route is exactly the case where inventing an answer would
        make this agree with the firewall by accident.
        """
        connected: List[Egress] = []
        for role, cidrs in interfaces:
            for cidr in cidrs:
                try:
                    iface = ipaddress.ip_interface(cidr)
                except ValueError:
                    continue
                connected.append(Egress(
                    prefix=ipaddress.ip_network(iface.network),
                    interface_role=role,
                    zone=zone_of_role.get(role),
                    via="connected",
                ))

        out = list(connected)
        for dest, nexthop in routes:
            try:
                dest_net = ipaddress.ip_network(dest, strict=False)
                nh = ipaddress.ip_address(nexthop)
            except ValueError:
                continue
            via_iface = None
            for c in connected:
                if nh in c.prefix and (via_iface is None
                                       or c.prefix.prefixlen > via_iface.prefix.prefixlen):
                    via_iface = c
            if via_iface is None:
                continue
            out.append(Egress(prefix=dest_net, interface_role=via_iface.interface_role,
                              zone=via_iface.zone, via="static"))
        return cls(egresses=tuple(out))


@dataclass(frozen=True)
class Mismatch:
    """A rule whose declared zone pair is not where its addresses route."""

    rule: str
    side: str                    # "source" or "destination"
    address: str
    declared_zone: str
    routed_zone: Optional[str]   # None = nothing in the repo routes it at all

    @property
    def reason(self) -> str:
        if self.routed_zone is None:
            return (f"{self.side} {self.address} matches no route this repository "
                    f"declares, so nothing says it is reachable at all — the rule "
                    f"claims zone {self.declared_zone!r}")
        return (f"{self.side} {self.address} routes out of zone "
                f"{self.routed_zone!r}, but the rule is placed on "
                f"{self.declared_zone!r} — it cannot match this traffic")


def check_rule(*, name: str, sources: Iterable[str], destinations: Iterable[str],
               from_zone: str, to_zone: str, view: RoutingView) -> List[Mismatch]:
    """Every address on this rule whose routed zone is not the declared one."""
    out: List[Mismatch] = []
    for side, addrs, declared in (("source", sources, from_zone),
                                  ("destination", destinations, to_zone)):
        for addr in addrs:
            routed = view.zone_for(addr)
            if routed != declared:
                out.append(Mismatch(rule=name, side=side, address=addr,
                                    declared_zone=declared, routed_zone=routed))
    return out
