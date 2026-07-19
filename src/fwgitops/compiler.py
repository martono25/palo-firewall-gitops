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
from typing import Any, Dict, List, Union

from fwgitops.intent import AccessRequest, Endpoint, Service
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


@dataclass(frozen=True)
class CompiledChange:
    address_objects: List[AddressObject]
    service_objects: List[ServiceObject]
    rule: SecurityRule


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

    rule = SecurityRule(
        name=ar.metadata.id,
        folder=res.folder,
        from_zones=[res.from_zone],
        to_zones=[res.to_zone],
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
    }


def dumps_tfvars(changes: List[CompiledChange]) -> str:
    """Byte-stable JSON for `rules.auto.tfvars.json`.

    sort_keys makes output deterministic across runs so re-compiles never churn
    the file and PR diffs reflect only real changes.
    """
    return json.dumps(to_tfvars(changes), sort_keys=True, indent=2) + "\n"
