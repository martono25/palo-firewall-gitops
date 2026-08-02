"""Phase-1 compiler: intent → PAN-OS objects + rule → rules.auto.tfvars.json.

Phase 1 is append-only (no dedup-vs-current, no sectioned placement, no risk
classifier — those are the Phase-2 analysis core). It turns a validated
AccessRequest into the data a static Terraform module consumes via `for_each`.

    AccessRequest ─▶ compile_request() ─▶ CompiledChange (objects + one rule)
         many changes ─▶ to_tfvars() ─▶ {address_objects, service_objects, security_rules}
                                    └─▶ dumps_tfvars() ─▶ byte-stable JSON (determinism)

Design decisions (see docs/DESIGN.md, eng review):
  * ONE rule per request — multiple sources/dests/services become member lists
    on a single security rule (keeps the 1:1 per-commit isolation).
  * Objects carry the managed marker ONLY; per-request provenance
    (req/section/ticket/expiry) lives on the RULE. Objects are shared and
    deterministically named, so identical values dedupe in the aggregate.
  * Deterministic + byte-stable output: same intents → identical JSON, so
    re-runs never churn and PR diffs stay clean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from fwgitops.intent import AccessRequest, Endpoint, Service, ZoneRequest
from fwgitops.resolve import EnvMap
from fwgitops.tags import MANAGED_TAG, Section, managed_tags, object_name


class CompileError(Exception):
    """Raised when a request cannot be compiled. Actionable message (fail closed)."""


@dataclass(frozen=True)
class AddressObject:
    name: str
    type: str  # "ip-netmask" | "fqdn"
    value: str
    folder: str
    tags: List[str] = field(default_factory=lambda: [MANAGED_TAG])


@dataclass(frozen=True)
class ServiceObject:
    name: str
    protocol: str
    port: str
    folder: str
    tags: List[str] = field(default_factory=lambda: [MANAGED_TAG])


@dataclass(frozen=True)
class SecurityRule:
    name: str
    folder: str
    from_zones: List[str]
    to_zones: List[str]
    sources: List[str]
    destinations: List[str]
    services: List[str]
    action: str
    log_end: bool
    tags: List[str]
    # ── ADR-0003 rule components (defaults = plain L4 allow, no inspection) ──
    application: List[str] = field(default_factory=lambda: ["any"])
    #: Security profile group name; None -> no threat inspection.
    profile_group: Optional[str] = None
    #: External log-forwarding profile name; None -> local logs only.
    log_setting: Optional[str] = None
    #: Rulebase (pre|post) — where managed policy lives above device-local rules.
    rulebase: str = "pre"
    #: Ordering within the rulebase: top | bottom | before | after.
    relative_position: str = "bottom"
    #: Anchor rule for before/after ordering; None for top/bottom.
    target_rule: Optional[str] = None
    # ── v1.0 rule completeness (all set via enrich; provider drops them too) ──
    description: Optional[str] = None
    log_start: bool = False
    source_user: List[str] = field(default_factory=lambda: ["any"])
    category: List[str] = field(default_factory=lambda: ["any"])
    negate_source: bool = False
    negate_destination: bool = False


@dataclass(frozen=True)
class CompiledChange:
    address_objects: List[AddressObject]
    service_objects: List[ServiceObject]
    rule: SecurityRule


@dataclass(frozen=True)
class CompiledZone:
    """Compiled output of a ZoneRequest (kind #2) — one scm_zone in a folder."""

    folder: str
    name: str
    zone_type: str
    interfaces: List[str]
    # Security posture — see ZoneSpec for why each one matters.
    protection_profile: Optional[str] = None
    log_forwarding: Optional[str] = None
    user_id: Optional[bool] = None
    device_id: Optional[bool] = None
    dos_profile: Optional[str] = None
    dos_log_forwarding: Optional[str] = None
    user_acl: Optional[Dict[str, List[str]]] = None
    device_acl: Optional[Dict[str, List[str]]] = None

    @property
    def has_protection(self) -> bool:
        """A zone with no protection profile has NO flood/recon protection."""
        return bool(self.protection_profile)


# NOTE: dispatch lives in `fwgitops.kinds`, not here. This module owns the
# per-kind compile FUNCTIONS (`compile_request`, `_compile_zone`); the registry
# owns choosing between them. Keeping an isinstance chain here as well would be
# a second place to update when adding a kind, which is the whole problem
# ADR-0001's registry exists to remove.


def _address_for(ep: Endpoint, folder: str) -> AddressObject:
    obj_type = "fqdn" if ep.kind == "fqdn" else "ip-netmask"
    return AddressObject(
        name=object_name("address", ep.value), type=obj_type, value=ep.value, folder=folder
    )


def _service_for(svc: Service, folder: str) -> ServiceObject:
    canonical = f"{svc.protocol}/{svc.port}"
    return ServiceObject(
        name=object_name("service", canonical), protocol=svc.protocol, port=svc.port, folder=folder
    )


def _parse_position(position: str) -> Tuple[str, Optional[str]]:
    """Intent position -> (relative_position, target_rule) for scm_security_rule.

    'top'/'bottom' -> (that, None); 'before:R'/'after:R' -> ('before'|'after', 'R').
    The intent loader has already validated the shape.
    """
    if position in ("top", "bottom"):
        return position, None
    rel, _, tgt = position.partition(":")
    return rel, (tgt.strip() or None)


def _zones_for(endpoints, default_zone: str) -> List[str]:
    """Zones for a rule side: each endpoint's zone (from its app) or the env
    default (explicit endpoints). Order-preserving dedup — PAN-OS allows a rule
    to list several from/to zones."""
    zones: List[str] = []
    for ep in endpoints:
        z = ep.zone or default_zone
        if z not in zones:
            zones.append(z)
    return zones


def compile_request(
    ar: AccessRequest,
    env_map: EnvMap,
    section: Section = Section.SPECIFIC_ALLOW,
) -> CompiledChange:
    """Compile one validated request into objects + a single security rule.

    Phase 1: env resolves to folder + zone-pair; endpoints become address
    objects; services become service objects; one rule references them all.
    """
    res = env_map.resolve(ar.spec.environment)  # raises ResolveError (fail closed)

    src_objs = [_address_for(ep, res.folder) for ep in ar.spec.source]
    dst_objs = [_address_for(ep, res.folder) for ep in ar.spec.destination]
    svc_objs = [_service_for(s, res.folder) for s in ar.spec.service]

    # Dedup within the request (a source and dest could share a CIDR) by name,
    # preserving first-seen order for stable output.
    address_objects = _dedup_by_name([*src_objs, *dst_objs])
    service_objects = _dedup_by_name(svc_objs)

    rel, tgt = _parse_position(ar.spec.position)
    rule = SecurityRule(
        name=ar.metadata.id,
        folder=res.folder,
        # Zones follow the traffic's endpoints: an app-resolved endpoint carries
        # its own zone; an explicit cidr/fqdn uses the environment default. This
        # is what makes multi-zone rules possible (Phase 2).
        from_zones=_zones_for(ar.spec.source, res.from_zone),
        to_zones=_zones_for(ar.spec.destination, res.to_zone),
        sources=_names_in_order(src_objs),
        destinations=_names_in_order(dst_objs),
        services=_names_in_order(svc_objs),
        action=ar.spec.action,
        log_end=ar.spec.log,
        tags=managed_tags(
            req_id=ar.metadata.id,
            section=section,
            ticket=ar.metadata.ticket,
            expires=ar.metadata.expires,
        ),
        application=list(ar.spec.application),
        profile_group=ar.spec.profile,
        log_setting=ar.spec.log_forwarding,
        relative_position=rel,
        target_rule=tgt,
        description=ar.spec.description,
        log_start=ar.spec.log_start,
        source_user=list(ar.spec.source_user),
        category=list(ar.spec.category),
        negate_source=ar.spec.negate_source,
        negate_destination=ar.spec.negate_destination,
    )
    return CompiledChange(
        address_objects=address_objects, service_objects=service_objects, rule=rule
    )


def _dedup_by_name(objs: List[Any]) -> List[Any]:
    seen: Dict[str, Any] = {}
    for o in objs:
        seen.setdefault(o.name, o)
    return list(seen.values())


def _names_in_order(objs: List[Any]) -> List[str]:
    out: List[str] = []
    for o in objs:
        if o.name not in out:
            out.append(o.name)
    return out


def to_tfvars(changes: List[CompiledChange]) -> Dict[str, Any]:
    """Aggregate compiled changes into the per-folder tfvars structure.

    Objects dedupe by name across changes (identical values collapse). Rules key
    by rule name (the stable for_each key). Raises on a genuine conflict — two
    different definitions sharing a name — rather than silently last-wins.
    """
    address_objects: Dict[str, Dict[str, Any]] = {}
    service_objects: Dict[str, Dict[str, Any]] = {}
    security_rules: Dict[str, Dict[str, Any]] = {}

    for ch in changes:
        for a in ch.address_objects:
            _merge_object(address_objects, a.name, _address_dict(a))
        for s in ch.service_objects:
            _merge_object(service_objects, s.name, _service_dict(s))
        if ch.rule.name in security_rules:
            raise CompileError(
                f"duplicate rule key {ch.rule.name!r} — two requests share metadata.id"
            )
        security_rules[ch.rule.name] = _rule_dict(ch.rule)

    return {
        "address_objects": address_objects,
        "service_objects": service_objects,
        "security_rules": security_rules,
    }


def _merge_object(bucket: Dict[str, Dict[str, Any]], name: str, payload: Dict[str, Any]) -> None:
    existing = bucket.get(name)
    if existing is not None and existing != payload:
        raise CompileError(
            f"object name collision on {name!r} with differing definitions "
            f"(deterministic naming should prevent this — investigate)"
        )
    bucket[name] = payload


def _address_dict(a: AddressObject) -> Dict[str, Any]:
    return {"name": a.name, "type": a.type, "value": a.value, "folder": a.folder, "tags": list(a.tags)}


def _service_dict(s: ServiceObject) -> Dict[str, Any]:
    return {
        "name": s.name, "protocol": s.protocol, "port": s.port,
        "folder": s.folder, "tags": list(s.tags),
    }


def _rule_dict(r: SecurityRule) -> Dict[str, Any]:
    return {
        "name": r.name, "folder": r.folder,
        "from_zones": list(r.from_zones), "to_zones": list(r.to_zones),
        "sources": list(r.sources), "destinations": list(r.destinations),
        "services": list(r.services), "action": r.action,
        "log_end": r.log_end, "tags": list(r.tags),
        "application": list(r.application),
        "profile_group": r.profile_group,
        "log_setting": r.log_setting,
        "rulebase": r.rulebase,
        "relative_position": r.relative_position,
        "target_rule": r.target_rule,
    }


def dumps_payload(payload: Dict[str, Any]) -> str:
    """Byte-stable JSON for any `*.auto.tfvars.json`.

    sort_keys makes output deterministic across runs so re-compiles never churn
    the file and PR diffs reflect only real changes. One serializer for every
    kind, so a new kind cannot accidentally pick a different byte format.
    """
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def dumps_tfvars(changes: List[CompiledChange]) -> str:
    """Byte-stable JSON for `rules.auto.tfvars.json`."""
    return dumps_payload(to_tfvars(changes))


# ── ZoneRequest (kind #2) compile + tfvars ─────────────────────────────────
def _acl_dict(acl) -> Optional[Dict[str, List[str]]]:
    """ZoneAcl -> the provider's include_list/exclude_list shape."""
    if acl is None:
        return None
    return {"include_list": list(acl.include), "exclude_list": list(acl.exclude)}


def _compile_zone(zr: ZoneRequest, env_map: EnvMap) -> CompiledZone:
    res = env_map.resolve(zr.spec.environment)  # raises ResolveError (fail closed)
    sp = zr.spec
    return CompiledZone(
        folder=res.folder, name=sp.zone,
        zone_type=sp.zone_type, interfaces=list(sp.interfaces),
        protection_profile=sp.protection_profile, log_forwarding=sp.log_forwarding,
        user_id=sp.user_id, device_id=sp.device_id,
        dos_profile=sp.dos_profile, dos_log_forwarding=sp.dos_log_forwarding,
        user_acl=_acl_dict(sp.user_acl), device_acl=_acl_dict(sp.device_acl),
    )


def zone_tfvars(zones: List[CompiledZone]) -> Dict[str, Any]:
    """Aggregate compiled zones into the per-folder `zones` tfvars structure.

    scm_zone's `network` selects the type: {layer3: [ifaces]} etc. — an empty
    interface list is a valid typed zone (references resolve; traffic needs ifaces).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for z in zones:
        if z.name in out:
            raise CompileError(f"duplicate zone key {z.name!r} — two ZoneRequests share metadata.id/zone")
        # Shape mirrors the scm_zone provider schema exactly: the protection
        # profile and log setting live INSIDE `network`; the identification
        # toggles, DoS fields and ACLs are top-level. Keys are always present
        # (null when unset) so the JSON is byte-stable across compiles and a
        # PR diff shows only real changes.
        out[z.name] = {
            "name": z.name,
            "folder": z.folder,
            "network": {
                z.zone_type: list(z.interfaces),
                "zone_protection_profile": z.protection_profile,
                "log_setting": z.log_forwarding,
            },
            "enable_user_identification": z.user_id,
            "enable_device_identification": z.device_id,
            "dos_profile": z.dos_profile,
            "dos_log_setting": z.dos_log_forwarding,
            "user_acl": z.user_acl,
            "device_acl": z.device_acl,
        }
    return {"zones": out}


def dumps_zone_tfvars(zones: List[CompiledZone]) -> str:
    """Byte-stable JSON for `zones.auto.tfvars.json` (terraform auto-loads it)."""
    return dumps_payload(zone_tfvars(zones))


def check_zone_consistency(
    changes: List[CompiledChange], zones: List[CompiledZone], env_map: EnvMap
) -> List[str]:
    """Cross-kind check: every zone a rule uses must be DECLARED for its folder.

    Declared = the env-map baseline (from_zone/to_zone, which exist on the device)
    PLUS any zone a ZoneRequest declares. So a rule referencing a zone (via an app
    or otherwise) that no ZoneRequest backs is caught HERE, at compile/PR time —
    not at the firewall's device commit ("zone 'X' is not a valid reference").
    Returns actionable violation messages (empty list = consistent).
    """
    declared: Dict[str, set] = {f: set(z) for f, z in env_map.baseline_zones_by_folder().items()}
    for z in zones:
        declared.setdefault(z.folder, set()).add(z.name)

    violations: List[str] = []
    for ch in sorted(changes, key=lambda c: c.rule.name):
        used = set(ch.rule.from_zones) | set(ch.rule.to_zones)
        undeclared = used - declared.get(ch.rule.folder, set())
        if undeclared:
            have = sorted(declared.get(ch.rule.folder, set())) or ["(none)"]
            violations.append(
                f"{ch.rule.name}: zone(s) {sorted(undeclared)} in folder {ch.rule.folder!r} "
                f"are not declared — add a ZoneRequest (or fix the app/env). Declared: {have}"
            )
    return violations


def check_zone_collisions(zones: List[CompiledZone], env_map: EnvMap) -> List[str]:
    """Reject a ZoneRequest naming a zone that ALREADY EXISTS on the device.

    `check_zone_consistency` UNIONS baseline and declared zones, so a ZoneRequest
    for an existing zone such as `internet` or `proxy` looks maximally valid
    there. But Terraform would then try to CREATE an object that already exists,
    and once zones carry security fields that create clobbers a live baseline
    zone's protection profile and logging.

    Baseline zones are not Terraform-managed. Adopting one is an import, not a
    create, and is a deliberate act — so this fails closed and says so.
    """
    baseline = env_map.baseline_zones_by_folder()
    violations: List[str] = []
    for z in sorted(zones, key=lambda z: (z.folder, z.name)):
        if z.name in baseline.get(z.folder, set()):
            violations.append(
                f"ZoneRequest {z.name!r} in folder {z.folder!r} names a zone that already "
                f"exists on the device (declared as a baseline zone). Creating it would "
                f"clobber the live zone's settings — remove the ZoneRequest and reference "
                f"the zone directly, or rename the new zone."
            )
    return violations
