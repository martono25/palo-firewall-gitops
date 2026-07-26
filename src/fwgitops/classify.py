"""Phase-2 risk classifier — policy-as-code, fail-closed.

Classifies a CompiledChange into a risk tier that drives the apply gate:

    LOW      -> auto-apply
    HIGH     -> human review
    CRITICAL -> dual-control, maintenance window

Only STATELESS checks live here — everything decidable from this one change.
Stateful checks that need the current policy (novel zone-pair, shadowing,
redundancy) share the SCM state model and land next.

Fail-closed is the whole point: anything we cannot confidently evaluate as LOW
ESCALATES. An unresolvable object, an unparseable CIDR/port, or an unexpected
shape yields at least HIGH — we never under-classify a change we don't
understand. The tier is the MAX severity of any fired check; nothing fired = LOW.

Output is an `evidence.RiskVerdict`, so the verdict drops straight into the
evidence bundle's `risk` section and the pipeline's tier gate. The classifier and
threshold versions are recorded for reproducibility (a verdict is only meaningful
against the ruleset that produced it).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from fwgitops.compiler import AddressObject, CompiledChange
from fwgitops.evidence import RiskVerdict

CLASSIFIER_VERSION = "1.0"

#: Tier ordering for "take the most severe fired check".
_ORDER = {"LOW": 0, "HIGH": 1, "CRITICAL": 2}

#: Zone names treated as the untrusted / internet side. Lowercased match.
INTERNET_ZONES: FrozenSet[str] = frozenset(
    {"internet", "untrust", "untrusted", "outside", "wan", "public"}
)


def _worst(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


@dataclass(frozen=True)
class Thresholds:
    """Versioned classifier knobs. A verdict is only comparable within a version."""

    version: str = "2026-07-26"
    #: prefix length <= this (i.e. FEWER host bits fixed) counts as "broad".
    broad_prefix_max: int = 16
    #: a service port range spanning MORE than this many ports is "wide".
    wide_port_span: int = 1024
    #: ports that are dangerous to expose inbound from the internet.
    risky_ports: FrozenSet[int] = frozenset(
        {22, 23, 135, 139, 445, 1433, 1521, 3306, 3389, 5432, 5900, 5985, 5986, 6379, 9200, 27017}
    )


DEFAULT_THRESHOLDS = Thresholds()


class _Unclassifiable(Exception):
    """A referenced object/value could not be parsed — force escalation."""


@dataclass(frozen=True)
class _Cidr:
    prefix: int          # 0..32 ; 0 == 0.0.0.0/0 (any)
    is_any: bool


def _resolve_addr(by_name: Dict[str, AddressObject], name: str) -> Optional[_Cidr]:
    """Resolve a rule's address reference to a CIDR, or None for an fqdn (specific).

    Raises _Unclassifiable if the name is unknown or the value cannot be parsed —
    we must not silently treat something we can't read as low risk.
    """
    obj = by_name.get(name)
    if obj is None:
        raise _Unclassifiable(f"address object {name!r} referenced by the rule was not found")
    if obj.type == "fqdn":
        return None  # a named host — treat as specific
    try:
        net = ipaddress.ip_network(obj.value, strict=False)
    except ValueError as e:
        raise _Unclassifiable(f"address {name!r} has an unparseable value {obj.value!r}: {e}")
    return _Cidr(prefix=net.prefixlen, is_any=(net.prefixlen == 0))


def _port_span(port: str) -> int:
    """Number of ports a service spec covers. Raises _Unclassifiable on bad input."""
    p = port.strip().lower()
    if p in ("any", "0-65535", "0"):
        return 65536
    try:
        if "-" in p:
            lo, hi = (int(x) for x in p.split("-", 1))
            return max(0, hi - lo) + 1
        return 1  # single port
    except ValueError as e:
        raise _Unclassifiable(f"service port {port!r} is not a number or range: {e}")


def _ports(port: str) -> List[int]:
    p = port.strip().lower()
    if "-" in p:
        lo, hi = (int(x) for x in p.split("-", 1))
        return list(range(lo, hi + 1))
    if p.isdigit():
        return [int(p)]
    return []  # "any" etc. handled by span, not membership


def classify(change: CompiledChange, *, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> RiskVerdict:
    """Classify a compiled change into a RiskVerdict (fail-closed)."""
    rule = change.rule
    by_name = {a.name: a for a in change.address_objects}
    fired: List[Dict[str, str]] = []

    def fire(tier: str, check: str, reason: str) -> None:
        fired.append({"check": check, "reason": reason, "tier": tier})

    try:
        # A deny rule reduces attack surface; the risky-open checks are for allow.
        allow = rule.action.lower() == "allow"
        from_internet = any(z.lower() in INTERNET_ZONES for z in rule.from_zones)
        to_internet = any(z.lower() in INTERNET_ZONES for z in rule.to_zones)

        srcs = [_resolve_addr(by_name, n) for n in rule.sources]
        dsts = [_resolve_addr(by_name, n) for n in rule.destinations]
        src_any = any(c is not None and c.is_any for c in srcs)
        dst_any = any(c is not None and c.is_any for c in dsts)

        if allow:
            # ── CRITICAL ──
            if src_any and dst_any:
                fire("CRITICAL", "any_any_allow", "source and destination are both 0.0.0.0/0")
            if from_internet and src_any:
                fire("CRITICAL", "inbound_any_from_internet",
                     "allows any source (0.0.0.0/0) inbound from an internet/untrust zone")

            # ── HIGH ──
            for tag, cidrs in (("source", srcs), ("destination", dsts)):
                for c in cidrs:
                    if c is not None and not c.is_any and c.prefix <= thresholds.broad_prefix_max:
                        fire("HIGH", f"broad_{tag}",
                             f"{tag} /{c.prefix} is broader than /{thresholds.broad_prefix_max}")
            for svc in change.service_objects:
                if _port_span(svc.port) > thresholds.wide_port_span:
                    fire("HIGH", "any_service",
                         f"service {svc.name} spans a wide port range ({svc.port})")
                if from_internet:
                    exposed = sorted(set(_ports(svc.port)) & thresholds.risky_ports)
                    if exposed:
                        fire("HIGH", "risky_port_from_internet",
                             f"exposes port(s) {exposed} inbound from an internet/untrust zone")
    except _Unclassifiable as e:
        # Fail closed: we could not fully evaluate the change -> never LOW.
        return RiskVerdict(
            tier="HIGH",
            classifier_version=CLASSIFIER_VERSION,
            thresholds_version=thresholds.version,
            checks_fired=({"check": "unclassifiable_input", "reason": str(e), "tier": "HIGH"},),
        )

    tier = "LOW"
    for f in fired:
        tier = _worst(tier, f["tier"])

    return RiskVerdict(
        tier=tier,
        classifier_version=CLASSIFIER_VERSION,
        thresholds_version=thresholds.version,
        checks_fired=tuple(fired),
    )
