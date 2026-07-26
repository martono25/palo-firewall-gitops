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

import re
from dataclasses import dataclass
from typing import Any, Dict, List

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
