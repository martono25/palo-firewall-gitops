"""Intent schema + loader/validator (Day-2 request surface, Phase 1).

Requesters speak app/business language; this module parses and validates an
intent document into typed objects the compiler can consume. It implements the
uniform fail-closed error contract (design task T5): every problem is collected
and surfaced with an actionable message, and a document with *any* problem never
produces a partial result.

    intent dict ──▶ load_intent() ──┬─ ok  ─▶ AccessRequest (typed)
                                    └─ bad ─▶ IntentError(problems[])  ──▶ PR comment

Phase-1 scope (see docs/DESIGN.md): catalog resolution is deferred, so the
`app:` endpoint form and the service `name:` form are not yet resolvable. They
are rejected with an actionable message pointing at the explicit forms
(`cidr:` / `fqdn:` / `protocol`+`port`) rather than silently accepted.

Zero external dependencies: validation uses `ipaddress` and `re` from the
stdlib. YAML parsing is the caller's job; `load_intent` takes a plain dict.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, List, Optional

API_VERSION = "fw-intent/v1"
KIND = "AccessRequest"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")  # flows into PAN-OS tags downstream
_FQDN = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$")
_ACTIONS = {"allow", "deny"}
_PROTOCOLS = {"tcp", "udp"}
_ENDPOINT_KEYS = {"cidr", "fqdn", "app"}


# ── Error contract ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Problem:
    """A single validation problem, addressed to a field path."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class IntentError(Exception):
    """Raised when an intent document is invalid. Carries every problem found.

    The string form is PR-comment ready — that is the fail-closed feedback the
    requester sees.
    """

    def __init__(self, problems: List[Problem]):
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"intent rejected ({len(problems)} problem(s)):\n{body}")


# ── Typed model ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Endpoint:
    kind: str  # "cidr" | "fqdn"  (an `app:` resolves into these)
    value: str
    #: The zone this endpoint sits in, from its app (Phase 2). None = use the
    #: environment default zone (explicit cidr/fqdn endpoints).
    zone: Optional[str] = None


@dataclass(frozen=True)
class Service:
    protocol: str  # "tcp" | "udp"
    port: str      # "443" or "8000-8100"


@dataclass(frozen=True)
class Metadata:
    id: str
    requester: str
    ticket: str
    justification: str
    requested: date
    expires: Optional[date] = None


@dataclass(frozen=True)
class Spec:
    environment: str
    action: str
    source: List[Endpoint]
    destination: List[Endpoint]
    service: List[Service]
    log: bool = True


@dataclass(frozen=True)
class AccessRequest:
    metadata: Metadata
    spec: Spec


@dataclass(frozen=True)
class ZoneSpec:
    environment: str
    zone: str
    zone_type: str          # layer3 | layer2 | virtual-wire | tap | external | tunnel
    interfaces: List[str]    # member interfaces (may be empty — an empty zone is valid)


@dataclass(frozen=True)
class ZoneRequest:
    """kind: ZoneRequest — declares a zone in a folder (ADR-0001 kind #2)."""

    metadata: Metadata
    spec: ZoneSpec


# ── Loader ────────────────────────────────────────────────────────────────
class _Collector:
    def __init__(self, service_catalog: Any = None, app_catalog: Any = None) -> None:
        self.problems: List[Problem] = []
        #: Optional ServiceCatalog (Phase 2). Enables the `service: name:` form.
        self.service_catalog = service_catalog
        #: Optional AppCatalog (Phase 2). Enables the `source/destination: app:` form.
        self.app_catalog = app_catalog

    def add(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))


def load_intent(data: Any, *, service_catalog: Any = None, app_catalog: Any = None) -> AccessRequest:
    """Parse + validate an intent dict, dispatching on `kind` (ADR-0001).

    The envelope (`apiVersion` + `kind`) is common to every kind; the kind-
    specific schema is validated by the registered loader. An unknown kind stops
    here — we cannot validate a spec against a schema we do not have. Raises a
    single IntentError with every problem; never returns a partial result.

    Catalogs (Phase 2) are passed through to the loader: `service_catalog`
    enables `service: - name: https`; `app_catalog` enables `source: - app: X`
    (resolving to the app's addresses/fqdns + zone). Without them only the
    explicit forms are accepted.
    """
    if not isinstance(data, dict):
        raise IntentError([Problem("$", "document must be a mapping")])

    kind = data.get("kind")
    loader = _KIND_LOADERS.get(kind)
    if loader is None:
        # Unknown kind: we can't validate a spec against a schema we don't have,
        # so report the envelope problems (apiVersion too) and stop.
        problems: List[Problem] = []
        if data.get("apiVersion") != API_VERSION:
            problems.append(Problem("apiVersion",
                                    f"must be {API_VERSION!r}, got {data.get('apiVersion')!r}"))
        problems.append(Problem("kind", f"unsupported kind {kind!r}; supported: {sorted(_KIND_LOADERS)}"))
        raise IntentError(problems)

    # Known kind: the loader validates apiVersion + the kind schema together, so a
    # requester sees every problem in one pass.
    return loader(data, service_catalog=service_catalog, app_catalog=app_catalog)


def _load_access_request(
    data: dict, *, service_catalog: Any = None, app_catalog: Any = None
) -> AccessRequest:
    """Loader for `kind: AccessRequest` — the security-rule kind (kind #1)."""
    c = _Collector(service_catalog=service_catalog, app_catalog=app_catalog)
    if data.get("apiVersion") != API_VERSION:
        c.add("apiVersion", f"must be {API_VERSION!r}, got {data.get('apiVersion')!r}")
    metadata = _load_metadata(data.get("metadata"), c)
    spec = _load_spec(data.get("spec"), c)
    if c.problems:
        raise IntentError(c.problems)
    # metadata and spec are non-None here because any failure added a problem.
    return AccessRequest(metadata=metadata, spec=spec)  # type: ignore[arg-type]


#: PAN-OS zone network types.
_ZONE_TYPES = {"layer3", "layer2", "virtual-wire", "tap", "external", "tunnel"}
ZONE_KIND = "ZoneRequest"


def _load_zone_spec(sp: Any, c: _Collector) -> Optional[ZoneSpec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None
    environment = _req_str(sp, "environment", path, c)
    zone = _req_str(sp, "zone", path, c)
    ztype = sp.get("type")
    if ztype not in _ZONE_TYPES:
        c.add(f"{path}.type", f"must be one of {sorted(_ZONE_TYPES)}, got {ztype!r}")
        ztype = None
    interfaces = sp.get("interfaces", [])
    if not isinstance(interfaces, list) or not all(
        isinstance(i, str) and i.strip() for i in interfaces
    ):
        c.add(f"{path}.interfaces", "must be a list of interface names (strings)")
        interfaces = None
    if environment is None or zone is None or ztype is None or interfaces is None:
        return None
    return ZoneSpec(environment=environment, zone=zone, zone_type=ztype, interfaces=list(interfaces))


def _load_zone_request(
    data: dict, *, service_catalog: Any = None, app_catalog: Any = None
) -> ZoneRequest:
    """Loader for `kind: ZoneRequest` (kind #2). Catalogs are unused (no names)."""
    c = _Collector()
    if data.get("apiVersion") != API_VERSION:
        c.add("apiVersion", f"must be {API_VERSION!r}, got {data.get('apiVersion')!r}")
    metadata = _load_metadata(data.get("metadata"), c)
    spec = _load_zone_spec(data.get("spec"), c)
    if c.problems:
        raise IntentError(c.problems)
    return ZoneRequest(metadata=metadata, spec=spec)  # type: ignore[arg-type]


#: Intent kind -> loader (ADR-0001). Register new kinds here; each loader
#: validates that kind's schema and returns its typed request. The pipeline
#: compiles/classifies per kind. Keeps the envelope check in one place.
_KIND_LOADERS = {KIND: _load_access_request, ZONE_KIND: _load_zone_request}


def _req_str(obj: dict, key: str, path: str, c: _Collector) -> Optional[str]:
    val = obj.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        c.add(f"{path}.{key}", "required")
        return None
    if not isinstance(val, str):
        c.add(f"{path}.{key}", f"must be a string, got {type(val).__name__}")
        return None
    return val


def _date(obj: dict, key: str, path: str, c: _Collector, required: bool) -> Optional[date]:
    val = obj.get(key)
    if val is None:
        if required:
            c.add(f"{path}.{key}", "required (ISO date, e.g. 2026-07-19)")
        return None
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val))
    except ValueError:
        c.add(f"{path}.{key}", f"invalid date {val!r}; expected ISO format YYYY-MM-DD")
        return None


def _load_metadata(md: Any, c: _Collector) -> Optional[Metadata]:
    path = "metadata"
    if not isinstance(md, dict):
        c.add(path, "required mapping")
        return None

    _id = _req_str(md, "id", path, c)
    if _id is not None and not _SAFE_ID.match(_id):
        c.add(f"{path}.id", f"{_id!r} must match {_SAFE_ID.pattern} (it becomes a firewall tag)")
    requester = _req_str(md, "requester", path, c)
    ticket = _req_str(md, "ticket", path, c)  # mandatory — audit linkage
    if ticket is not None and not _SAFE_ID.match(ticket):
        c.add(f"{path}.ticket", f"{ticket!r} must match {_SAFE_ID.pattern} (it becomes a tag)")
    justification = _req_str(md, "justification", path, c)
    requested = _date(md, "requested", path, c, required=True)
    expires = _date(md, "expires", path, c, required=False)

    if None in (_id, requester, ticket, justification, requested):
        return None
    return Metadata(
        id=_id, requester=requester, ticket=ticket,  # type: ignore[arg-type]
        justification=justification, requested=requested, expires=expires,  # type: ignore[arg-type]
    )


def _load_spec(sp: Any, c: _Collector) -> Optional[Spec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None

    environment = _req_str(sp, "environment", path, c)
    action = sp.get("action")
    if action not in _ACTIONS:
        c.add(f"{path}.action", f"must be one of {sorted(_ACTIONS)}, got {action!r}")

    source = _load_endpoints(sp.get("source"), f"{path}.source", c, environment)
    destination = _load_endpoints(sp.get("destination"), f"{path}.destination", c, environment)
    service = _load_services(sp.get("service"), f"{path}.service", c)

    log = sp.get("log", True)
    if not isinstance(log, bool):
        c.add(f"{path}.log", f"must be a boolean, got {type(log).__name__}")

    if environment is None or action not in _ACTIONS or source is None or destination is None \
            or service is None or not isinstance(log, bool):
        return None
    return Spec(
        environment=environment, action=action, source=source,
        destination=destination, service=service, log=log,
    )


def _load_endpoints(
    raw: Any, path: str, c: _Collector, environment: Optional[str]
) -> Optional[List[Endpoint]]:
    if not isinstance(raw, list) or not raw:
        c.add(path, "required non-empty list")
        return None
    out: List[Endpoint] = []
    ok = True
    for i, item in enumerate(raw):
        eps = _load_endpoint(item, f"{path}[{i}]", c, environment)
        if eps is None:
            ok = False
        else:
            out.extend(eps)  # an app expands to several endpoints
    return out if ok else None


def _load_endpoint(
    item: Any, path: str, c: _Collector, environment: Optional[str]
) -> Optional[List[Endpoint]]:
    if not isinstance(item, dict):
        c.add(path, "must be a mapping with exactly one of cidr/fqdn/app")
        return None
    keys = _ENDPOINT_KEYS & set(item)
    if len(keys) != 1:
        c.add(path, f"must have exactly one of {sorted(_ENDPOINT_KEYS)}, found {sorted(keys)}")
        return None
    kind = next(iter(keys))
    value = item[kind]
    if not isinstance(value, str) or not value.strip():
        c.add(f"{path}.{kind}", "must be a non-empty string")
        return None

    if kind == "app":
        # Phase-2 friendly form: resolve to the app's endpoints, tagged with its zone.
        from fwgitops.catalog import CatalogError  # local import: no hard dep in Phase 1

        if c.app_catalog is None:
            c.add(f"{path}.app",
                  "no app catalog loaded — use explicit 'cidr:'/'fqdn:', or provide catalog/apps.yaml")
            return None
        try:
            app = c.app_catalog.resolve(value)
        except CatalogError as e:
            c.add(f"{path}.app", str(e))
            return None
        if environment is not None and app.environment != environment:
            c.add(f"{path}.app",
                  f"app {value!r} is in environment {app.environment!r}, "
                  f"but this request is for {environment!r}")
            return None
        eps = [Endpoint(kind="cidr", value=a, zone=app.zone) for a in app.addresses]
        eps += [Endpoint(kind="fqdn", value=f, zone=app.zone) for f in app.fqdns]
        return eps
    if kind == "cidr":
        try:
            ipaddress.ip_network(value, strict=True)
        except ValueError as e:
            hint = " (host bits set — did you mean the network address?)" if "host bits" in str(e) else ""
            c.add(f"{path}.cidr", f"invalid CIDR {value!r}{hint}")
            return None
    elif kind == "fqdn":
        if not _FQDN.match(value):
            c.add(f"{path}.fqdn", f"invalid FQDN {value!r}")
            return None
    return [Endpoint(kind=kind, value=value)]  # explicit endpoint: env-default zone


def _load_services(raw: Any, path: str, c: _Collector) -> Optional[List[Service]]:
    if not isinstance(raw, list) or not raw:
        c.add(path, "required non-empty list")
        return None
    out: List[Service] = []
    ok = True
    for i, item in enumerate(raw):
        svc = _load_service(item, f"{path}[{i}]", c)
        if svc is None:
            ok = False
        else:
            out.append(svc)
    return out if ok else None


def _load_service(item: Any, path: str, c: _Collector) -> Optional[Service]:
    if not isinstance(item, dict):
        c.add(path, "must be a mapping with protocol+port (or a catalog name)")
        return None
    if "name" in item:
        # Phase-2 friendly form: resolve the name through the service catalog.
        from fwgitops.catalog import CatalogError  # local import: no hard dep in Phase 1

        if c.service_catalog is None:
            c.add(f"{path}.name",
                  "no service catalog loaded — use explicit 'protocol' + 'port', "
                  "or provide catalog/services.yaml")
            return None
        try:
            sd = c.service_catalog.resolve(item["name"])
        except CatalogError as e:
            c.add(f"{path}.name", str(e))
            return None
        return Service(protocol=sd.protocol, port=sd.port)
    protocol = item.get("protocol")
    if protocol not in _PROTOCOLS:
        c.add(f"{path}.protocol", f"must be one of {sorted(_PROTOCOLS)}, got {protocol!r}")
        protocol = None
    port = item.get("port")
    port_str = _validate_port(port, f"{path}.port", c)
    if protocol is None or port_str is None:
        return None
    return Service(protocol=protocol, port=port_str)


def _validate_port(port: Any, path: str, c: _Collector) -> Optional[str]:
    if port is None:
        c.add(path, "required (e.g. 443 or 8000-8100)")
        return None
    s = str(port)
    parts = s.split("-")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        c.add(path, f"invalid port {port!r}; expected N or N-M within 1-65535")
        return None
    if len(nums) not in (1, 2) or any(not (1 <= n <= 65535) for n in nums):
        c.add(path, f"port {port!r} out of range; each must be 1-65535")
        return None
    if len(nums) == 2 and nums[0] >= nums[1]:
        c.add(path, f"port range {port!r} must be ascending (low-high)")
        return None
    return s
