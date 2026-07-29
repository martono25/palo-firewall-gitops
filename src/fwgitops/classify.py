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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from fwgitops.compiler import AddressObject, CompiledChange, SecurityRule
from fwgitops.evidence import RiskVerdict

CLASSIFIER_VERSION = "1.0"

#: Tiers in ascending severity — the public ordering for gates/comparisons.
TIERS = ("LOW", "HIGH", "CRITICAL")

#: Tier ordering for "take the most severe fired check".
_ORDER = {t: i for i, t in enumerate(TIERS)}

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


#: A rule's normalized match signature (folder-scoped). Object names are
#: value-derived (deterministic), so equal signatures == equal effective match.
_Sig = Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], str]


def _rule_sig(rule: SecurityRule) -> _Sig:
    return (
        tuple(sorted(rule.from_zones)), tuple(sorted(rule.to_zones)),
        tuple(sorted(rule.sources)), tuple(sorted(rule.destinations)),
        tuple(sorted(rule.services)), rule.action,
    )


@dataclass(frozen=True)
class RuleFacts:
    """A rule's resolved match — object VALUES (CIDR/fqdn) + (proto, port) — for
    subset/containment analysis (shadowing)."""

    rid: str
    folder: str
    from_zones: FrozenSet[str]
    to_zones: FrozenSet[str]
    action: str
    sources: FrozenSet[str]                     # address object values (CIDR or fqdn)
    dests: FrozenSet[str]
    services: FrozenSet[Tuple[str, str]]        # (protocol, port)


def _rule_facts(change: CompiledChange) -> RuleFacts:
    r = change.rule
    addr = {a.name: a.value for a in change.address_objects}
    svc = {s.name: (s.protocol, s.port) for s in change.service_objects}
    return RuleFacts(
        rid=r.name, folder=r.folder,
        from_zones=frozenset(r.from_zones), to_zones=frozenset(r.to_zones), action=r.action,
        sources=frozenset(addr.get(n, n) for n in r.sources),
        dests=frozenset(addr.get(n, n) for n in r.destinations),
        services=frozenset(svc.get(n, (n, "")) for n in r.services),
    )


def _covers(a_values: FrozenSet[str], b_value: str) -> bool:
    """Does some value in `a_values` cover `b_value`? Exact, any (0.0.0.0/0), or
    CIDR supernet. An fqdn is covered only exactly or by any (conservative)."""
    if b_value in a_values or "0.0.0.0/0" in a_values:
        return True
    try:
        bnet = ipaddress.ip_network(b_value, strict=False)
    except ValueError:
        return False  # fqdn / unparsable — only exact or any (handled above)
    for a in a_values:
        try:
            if bnet.subnet_of(ipaddress.ip_network(a, strict=False)):
                return True
        except ValueError:
            continue
    return False


def _all_covered(a_values: FrozenSet[str], b_values: FrozenSet[str]) -> bool:
    return all(_covers(a_values, b) for b in b_values)


def _port_bounds(port: str) -> Optional[Tuple[int, int]]:
    p = port.strip().lower()
    if p in ("any", "0-65535", "0"):
        return (0, 65535)
    try:
        if "-" in p:
            lo, hi = (int(x) for x in p.split("-", 1))
            return (lo, hi)
        return (int(p), int(p))
    except ValueError:
        return None


def _svc_covered(a_svcs: FrozenSet[Tuple[str, str]], b_svc: Tuple[str, str]) -> bool:
    """A covers b if some a-service has the same protocol and a port range ⊇ b's."""
    bp, bport = b_svc
    bb = _port_bounds(bport)
    if bb is None:
        return b_svc in a_svcs  # unparsable -> exact only
    for ap, aport in a_svcs:
        if ap != bp:
            continue
        ab = _port_bounds(aport)
        if ab and ab[0] <= bb[0] and bb[1] <= ab[1]:
            return True
    return False


def _all_svc_covered(a_svcs: FrozenSet[Tuple[str, str]], b_svcs: FrozenSet[Tuple[str, str]]) -> bool:
    return all(_svc_covered(a_svcs, b) for b in b_svcs)


@dataclass(frozen=True)
class PolicyContext:
    """The rest of the declared policy a change is judged against.

    GitOps is source of truth, so "current policy" = all the other committed
    intents. Built once per run; each change is then classified against it. Keyed
    by folder (rules in different folders are independent), and by RULE ID so a
    change is never compared against itself (a rule doesn't make its own zone-pair
    "established" or itself "redundant").
    """

    #: (folder, (from_zone, to_zone)) -> ids of rules using that pair
    zone_pair_rules: Dict[Tuple[str, Tuple[str, str]], FrozenSet[str]]
    #: (folder, signature) -> ids of rules with that exact match
    sig_rules: Dict[Tuple[str, _Sig], FrozenSet[str]]
    #: resolved facts per rule, for containment (shadowing) analysis
    rule_facts: Tuple[RuleFacts, ...] = ()

    @classmethod
    def from_changes(cls, changes: Iterable[CompiledChange]) -> "PolicyContext":
        zp: Dict[Tuple[str, Tuple[str, str]], Set[str]] = defaultdict(set)
        sg: Dict[Tuple[str, _Sig], Set[str]] = defaultdict(set)
        facts: List[RuleFacts] = []
        for ch in changes:
            r = ch.rule
            for fz in r.from_zones:
                for tz in r.to_zones:
                    zp[(r.folder, (fz, tz))].add(r.name)
            sg[(r.folder, _rule_sig(r))].add(r.name)
            facts.append(_rule_facts(ch))
        return cls(
            zone_pair_rules={k: frozenset(v) for k, v in zp.items()},
            sig_rules={k: frozenset(v) for k, v in sg.items()},
            rule_facts=tuple(facts),
        )


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


def classify(
    change: CompiledChange,
    *,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    policy: Optional[PolicyContext] = None,
) -> RiskVerdict:
    """Classify a compiled change into a RiskVerdict (fail-closed).

    Stateless checks always run. If `policy` (the rest of the declared policy) is
    given, STATEFUL checks also run: `novel_zone_pair` (this rule uses a from/to
    zone-pair no OTHER rule in its folder uses — a new traffic path, HIGH) and
    `redundant_rule` (its match is identical to another rule — sprawl, LOW note).
    """
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

            # ── Negation (v1.0) ──
            # A negated source/destination inverts the match ("everything EXCEPT
            # X"), so the CIDR-breadth analysis above is not valid — a negated
            # specific source on an allow can be extremely broad. Fail closed: we
            # cannot confidently call a negated allow low-risk.
            if rule.negate_source or rule.negate_destination:
                sides = [s for s, on in (("source", rule.negate_source),
                                         ("destination", rule.negate_destination)) if on]
                fire("HIGH", "negated_match",
                     f"negated {', '.join(sides)} inverts the match — breadth analysis is "
                     "not valid, so this cannot be auto-classified as low risk")

            # ── Inspection posture (ADR-0003) ──
            # An allow with no security profile group permits traffic with zero
            # threat inspection (no IPS/AV/anti-spyware/URL/WildFire). Not blocked
            # — profiles are opt-in by design — but recorded so the evidence
            # bundle shows a human accepted an uninspected allow.
            if rule.profile_group is None:
                fire("LOW", "allow_without_inspection",
                     "allow rule has no security profile group — traffic is permitted "
                     "without threat inspection")
    except _Unclassifiable as e:
        # Fail closed: we could not fully evaluate the change -> never LOW.
        return RiskVerdict(
            tier="HIGH",
            classifier_version=CLASSIFIER_VERSION,
            thresholds_version=thresholds.version,
            checks_fired=({"check": "unclassifiable_input", "reason": str(e), "tier": "HIGH"},),
        )

    # ── Stateful checks (this change vs the rest of the declared policy) ──────
    if policy is not None:
        rid = rule.name
        novel = [
            f"{fz}->{tz}"
            for fz in rule.from_zones for tz in rule.to_zones
            if not (policy.zone_pair_rules.get((rule.folder, (fz, tz)), frozenset()) - {rid})
        ]
        if novel:
            fire("HIGH", "novel_zone_pair",
                 f"zone-pair(s) {novel} used by no other rule in folder {rule.folder!r} "
                 "(a new traffic path)")
        dupes = sorted(policy.sig_rules.get((rule.folder, _rule_sig(rule)), frozenset()) - {rid})
        if dupes:
            fire("LOW", "redundant_rule",
                 f"identical match to existing rule(s) {dupes} — possible duplicate/sprawl")
        elif rule.action.lower() == "allow":
            # Shadowed: this allow's traffic is a proper SUBSET of a broader allow
            # in the same folder (same zones-superset + covered sources/dests/
            # services) -> the broader rule already permits it (dead/redundant).
            me = _rule_facts(change)
            for other in policy.rule_facts:
                if other.rid == rid or other.folder != me.folder or other.action.lower() != "allow":
                    continue
                if (me.from_zones <= other.from_zones and me.to_zones <= other.to_zones
                        and _all_covered(other.sources, me.sources)
                        and _all_covered(other.dests, me.dests)
                        and _all_svc_covered(other.services, me.services)):
                    fire("LOW", "shadowed_by",
                         f"traffic is a subset of rule {other.rid!r} — likely redundant")
                    break

    tier = "LOW"
    for f in fired:
        tier = _worst(tier, f["tier"])

    return RiskVerdict(
        tier=tier,
        classifier_version=CLASSIFIER_VERSION,
        thresholds_version=thresholds.version,
        checks_fired=tuple(fired),
    )
