"""Service catalog — resolve friendly service names to protocol+port (Phase 2).

Lets an intent say `service: - name: https` instead of the explicit
`protocol: tcp, port: 443`. The catalog is platform-maintained config
(`catalog/services.yaml`); adding a name is a reviewed PR, so the friendly names
stay curated. Fail-closed: an unknown name or a malformed entry is an error,
never a silent default.

Kept free of any `fwgitops.intent` import so there is no cycle — the intent
loader imports this, converts a resolved `ServiceDef` into its own `Service`, and
owns the parse/validate flow.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Tuple

_PROTOCOLS = {"tcp", "udp"}
_PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")


class CatalogError(Exception):
    """The service catalog is malformed, or a requested name is unknown."""


def _valid_port(port: Any) -> str:
    """Return a normalized port string, or raise CatalogError."""
    s = str(port).strip() if isinstance(port, (str, int)) else None
    if not s or not _PORT_RE.match(s):
        raise CatalogError(f"invalid port {port!r} (expected '443' or '8000-8100')")
    parts = [int(p) for p in s.split("-")]
    for p in parts:
        if not 0 <= p <= 65535:
            raise CatalogError(f"port {p} out of range 0-65535")
    if len(parts) == 2 and parts[0] > parts[1]:
        raise CatalogError(f"port range {s} is inverted (low > high)")
    return s


@dataclass(frozen=True)
class ServiceDef:
    protocol: str
    port: str


@dataclass(frozen=True)
class ServiceCatalog:
    services: Dict[str, ServiceDef]

    def resolve(self, name: str) -> ServiceDef:
        svc = self.services.get(name)
        if svc is None:
            raise CatalogError(
                f"unknown service name {name!r}; known: {sorted(self.services) or '(catalog is empty)'}"
            )
        return svc

    @classmethod
    def from_dict(cls, raw: Any) -> "ServiceCatalog":
        """Build a catalog from a mapping. Accepts {services: {...}} or a bare map."""
        if not isinstance(raw, dict):
            raise CatalogError("service catalog must be a mapping")
        entries = raw.get("services", raw) if "services" in raw else raw
        if not isinstance(entries, dict):
            raise CatalogError("'services' must be a mapping of name -> {protocol, port}")

        out: Dict[str, ServiceDef] = {}
        problems: List[str] = []
        for name, spec in entries.items():
            if not isinstance(spec, dict):
                problems.append(f"{name}: must be a mapping with protocol + port")
                continue
            proto = spec.get("protocol")
            if proto not in _PROTOCOLS:
                problems.append(f"{name}.protocol: must be one of {sorted(_PROTOCOLS)}, got {proto!r}")
            try:
                port = _valid_port(spec.get("port"))
            except CatalogError as e:
                problems.append(f"{name}.port: {e}")
                port = None
            if proto in _PROTOCOLS and port is not None:
                out[str(name)] = ServiceDef(protocol=proto, port=port)
        if problems:
            raise CatalogError("invalid service catalog:\n  - " + "\n  - ".join(problems))
        return cls(services=out)


@dataclass(frozen=True)
class NameCatalog:
    """A curated allowlist of named firewall references (validation-only).

    Unlike Service/App catalogs, there is no expansion — a NameCatalog answers a
    single question: does this name exist on the firewall? It backs the ADR-0003
    `profile` (security profile group), `application` (App-ID), and
    `log_forwarding` (log-forwarding profile) fields, so a typo'd name is caught
    at PR time instead of at the device commit ("profile 'X' is not a valid
    reference"). Names are platform-maintained config — adding one is a reviewed
    PR — which keeps the reference surface curated. Fail-closed: an unknown name
    is an error, never a silent default.

    `always_valid` holds names that are always accepted without being listed
    (e.g. the built-in App-ID `any`).
    """

    kind: str                                      # human label, e.g. "App-ID"
    names: FrozenSet[str]
    always_valid: FrozenSet[str] = frozenset()

    def validate(self, name: str) -> None:
        """Raise CatalogError if `name` is not a known (or always-valid) name."""
        if name in self.always_valid or name in self.names:
            return
        known = sorted(self.names) or "(catalog is empty)"
        raise CatalogError(f"unknown {self.kind} {name!r}; known: {known}")

    @classmethod
    def from_dict(
        cls, raw: Any, *, kind: str, key: str, always_valid: FrozenSet[str] = frozenset()
    ) -> "NameCatalog":
        """Build from a mapping `{<key>: {name: {...}}}`, `{<key>: [name, ...]}`,
        or a bare mapping/list of names. Metadata per name is accepted and ignored
        (room to grow); only the name is validated."""
        if isinstance(raw, dict):
            entries: Any = raw.get(key, raw) if key in raw else raw
        else:
            entries = raw
        if isinstance(entries, dict):
            items = list(entries.keys())
        elif isinstance(entries, list):
            items = entries
        else:
            raise CatalogError(f"'{key}' must be a mapping or a list of names")

        out: set = set()
        problems: List[str] = []
        for name in items:
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{name!r}: name must be a non-empty string")
                continue
            out.add(name.strip())
        if problems:
            raise CatalogError(f"invalid {kind} catalog:\n  - " + "\n  - ".join(problems))
        return cls(kind=kind, names=frozenset(out), always_valid=always_valid)


@dataclass(frozen=True)
class AppDef:
    """A named application: its environment/folder/zone and where it lives."""

    environment: str
    folder: str
    zone: str
    addresses: Tuple[str, ...]  # CIDRs (network addresses; strict)
    fqdns: Tuple[str, ...]


@dataclass(frozen=True)
class AppCatalog:
    """Resolve `source: - app: web-tier` to its addresses/fqdns AND its zone.

    Unlike services, an app carries a ZONE — resolving apps is what lets a rule's
    from/to zones be derived from the traffic's actual endpoints (web-tier=trust,
    payments-api=app) instead of one fixed env-map pair. That is what unlocks
    multi-zone rules (and, later, the stateful novel-zone-pair classifier check).
    """

    apps: Dict[str, AppDef]

    def resolve(self, name: str) -> AppDef:
        app = self.apps.get(name)
        if app is None:
            raise CatalogError(
                f"unknown app name {name!r}; known: {sorted(self.apps) or '(catalog is empty)'}"
            )
        return app

    @classmethod
    def from_dict(cls, raw: Any) -> "AppCatalog":
        if not isinstance(raw, dict):
            raise CatalogError("app catalog must be a mapping")
        entries = raw.get("apps", raw) if "apps" in raw else raw
        if not isinstance(entries, dict):
            raise CatalogError("'apps' must be a mapping of name -> {environment, folder, zone, ...}")

        out: Dict[str, AppDef] = {}
        problems: List[str] = []
        for name, spec in entries.items():
            if not isinstance(spec, dict):
                problems.append(f"{name}: must be a mapping")
                continue
            env, folder, zone = spec.get("environment"), spec.get("folder"), spec.get("zone")
            strs_ok = True
            for field, val in (("environment", env), ("folder", folder), ("zone", zone)):
                if not isinstance(val, str) or not val.strip():
                    problems.append(f"{name}.{field}: required non-empty string")
                    strs_ok = False

            addresses = spec.get("addresses") or []
            fqdns = spec.get("fqdns") or []
            if not isinstance(addresses, list):
                problems.append(f"{name}.addresses: must be a list of CIDRs")
                addresses = []
            if not isinstance(fqdns, list):
                problems.append(f"{name}.fqdns: must be a list of FQDNs")
                fqdns = []

            valid_addr: List[str] = []
            for a in addresses:
                try:
                    ipaddress.ip_network(a, strict=True)
                    valid_addr.append(str(a))
                except (ValueError, TypeError) as e:
                    problems.append(f"{name}.addresses: invalid CIDR {a!r} ({e})")
            if not addresses and not fqdns:
                problems.append(f"{name}: must define at least one address or fqdn")

            if strs_ok:
                out[str(name)] = AppDef(
                    environment=env, folder=folder, zone=zone,
                    addresses=tuple(valid_addr), fqdns=tuple(str(f) for f in fqdns),
                )
        if problems:
            raise CatalogError("invalid app catalog:\n  - " + "\n  - ".join(problems))
        return cls(apps=out)
