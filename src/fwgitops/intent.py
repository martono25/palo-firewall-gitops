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
_ACTIONS = {"allow", "deny", "drop", "reset-client", "reset-server", "reset-both"}
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
    #: App-ID match. Omitted -> ["any"] (L4/port-only rule). ADR-0003.
    application: List[str] = field(default_factory=lambda: ["any"])
    #: Security profile GROUP name. None -> no threat inspection (plain allow).
    profile: Optional[str] = None
    #: External log-forwarding profile name. None -> local logs only.
    log_forwarding: Optional[str] = None
    #: Ordering: top | bottom | before:<rule> | after:<rule>. Default bottom.
    position: str = "bottom"
    # ── v1.0 rule completeness ──
    #: Free-text rule documentation (audit). None -> no description.
    description: Optional[str] = None
    #: Log at session start too (pairs with `log`, which is session end).
    log_start: bool = False
    #: User-ID match (users/groups). Omitted -> ["any"].
    source_user: List[str] = field(default_factory=lambda: ["any"])
    #: URL categories to match. Omitted -> ["any"].
    category: List[str] = field(default_factory=lambda: ["any"])
    #: Invert the source match (applies to everything EXCEPT source).
    negate_source: bool = False
    #: Invert the destination match.
    negate_destination: bool = False


@dataclass(frozen=True)
class AccessRequest:
    metadata: Metadata
    spec: Spec


@dataclass(frozen=True)
class ZoneAcl:
    """User-ID / device-ID include+exclude lists for a zone."""

    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ZoneSpec:
    environment: str
    zone: str
    zone_type: str          # layer3 | layer2 | virtual-wire | tap | external | tunnel
    interfaces: List[str]    # member interfaces (may be empty — an empty zone is valid)

    # ── Security posture. The ADR-0003 lesson applied to zones ────────────
    # A zone is not just a name and a port list. Verified live 2026-07-31 that
    # the scm provider writes all of these faithfully (unlike security rules,
    # which need `enrich`) and that SCM reference-validates the profile names.
    #
    #: Zone PROTECTION profile (flood / reconnaissance / packet-based attacks).
    #: Distinct from a rule's `profile`, which is a security profile GROUP.
    #: Absent means the zone has no such protection at all — the classifier
    #: flags that rather than treating it as fine.
    protection_profile: Optional[str] = None
    #: Log-forwarding profile. Same vocabulary as a rule's `log_forwarding`.
    log_forwarding: Optional[str] = None
    #: User-ID must be on PER ZONE or a rule matching `source_user` never
    #: matches — silently, because the rule is simply skipped.
    user_id: Optional[bool] = None
    device_id: Optional[bool] = None
    dos_profile: Optional[str] = None
    dos_log_forwarding: Optional[str] = None
    user_acl: Optional[ZoneAcl] = None
    device_acl: Optional[ZoneAcl] = None


@dataclass(frozen=True)
class InterfaceSpec:
    """kind: InterfaceRequest (ADR-0001 kind #3) — CONFIGURE an existing interface.

    ADR-0005: this does NOT create an interface. On the pilot tenant the
    interfaces already exist, named as folder-scope variables (`$eth-local`,
    `$eth-internet`) defined in the shared parent and inherited, each carrying a
    per-device `default_value`. What is missing is addressing — `layer3` is `{}`
    on every one of them. So an InterfaceRequest fills that in.

    Exactly one addressing mode may be set. The provider says so
    ("You must specify exactly one of dhcp_client, ip, and pppoe") and the
    device commit would say so too, later and less helpfully.
    """

    environment: str
    interface: str                       # the folder-scope name, e.g. "$eth-local"
    #: Static addressing: CIDRs, e.g. ["10.0.1.1/24"]. Mutually exclusive with dhcp.
    ip: List[str] = field(default_factory=list)
    #: DHCP client. Mutually exclusive with `ip`.
    dhcp: bool = False
    mtu: Optional[int] = None
    comment: Optional[str] = None
    #: Interface management profile — which admin services answer on this
    #: interface. A reference name, catalog-validated like a rule's profile.
    management_profile: Optional[str] = None


@dataclass(frozen=True)
class InterfaceRequest:
    """kind: InterfaceRequest — configures one interface in a folder (kind #3)."""

    metadata: Metadata
    spec: InterfaceSpec


@dataclass(frozen=True)
class ZoneRequest:
    """kind: ZoneRequest — declares a zone in a folder (ADR-0001 kind #2)."""

    metadata: Metadata
    spec: ZoneSpec


# ── Loader ────────────────────────────────────────────────────────────────
class _Collector:
    def __init__(
        self,
        service_catalog: Any = None,
        app_catalog: Any = None,
        profile_catalog: Any = None,
        application_catalog: Any = None,
        log_forwarding_catalog: Any = None,
        zone_protection_catalog: Any = None,
        interface_profile_catalog: Any = None,
    ) -> None:
        self.problems: List[Problem] = []
        #: Optional ServiceCatalog (Phase 2). Enables the `service: name:` form.
        self.service_catalog = service_catalog
        #: Optional AppCatalog (Phase 2). Enables the `source/destination: app:` form.
        self.app_catalog = app_catalog
        #: Optional NameCatalogs (ADR-0003). When present, validate the rule's
        #: profile / application / log_forwarding names against the firewall's
        #: known references — a typo fails here, not at the device commit.
        self.profile_catalog = profile_catalog
        self.application_catalog = application_catalog
        self.log_forwarding_catalog = log_forwarding_catalog
        #: Zone PROTECTION profiles (ZoneRequest) — flood/recon protection.
        self.zone_protection_catalog = zone_protection_catalog
        #: Interface management profiles (InterfaceRequest).
        self.interface_profile_catalog = interface_profile_catalog

    def add(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))


def load_intent(
    data: Any,
    *,
    service_catalog: Any = None,
    app_catalog: Any = None,
    profile_catalog: Any = None,
    application_catalog: Any = None,
    log_forwarding_catalog: Any = None,
    zone_protection_catalog: Any = None,
    interface_profile_catalog: Any = None,
) -> AccessRequest:
    """Parse + validate an intent dict, dispatching on `kind` (ADR-0001).

    The envelope (`apiVersion` + `kind`) is common to every kind; the kind-
    specific schema is validated by the registered loader. An unknown kind stops
    here — we cannot validate a spec against a schema we do not have. Raises a
    single IntentError with every problem; never returns a partial result.

    Catalogs (Phase 2) are passed through to the loader: `service_catalog`
    enables `service: - name: https`; `app_catalog` enables `source: - app: X`
    (resolving to the app's addresses/fqdns + zone). The ADR-0003 name catalogs
    (`profile_catalog`, `application_catalog`, `log_forwarding_catalog`) validate
    the rule's reference names. Any catalog left None disables only its check —
    the corresponding field is then accepted as free-form (back-compat).
    """
    if not isinstance(data, dict):
        raise IntentError([Problem("$", "document must be a mapping")])

    catalogs = dict(
        service_catalog=service_catalog, app_catalog=app_catalog,
        profile_catalog=profile_catalog, application_catalog=application_catalog,
        log_forwarding_catalog=log_forwarding_catalog,
        zone_protection_catalog=zone_protection_catalog,
        interface_profile_catalog=interface_profile_catalog,
    )
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
    return loader(data, **catalogs)


def _load_access_request(data: dict, **catalogs: Any) -> AccessRequest:
    """Loader for `kind: AccessRequest` — the security-rule kind (kind #1)."""
    c = _Collector(**catalogs)
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
INTERFACE_KIND = "InterfaceRequest"


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
    protection_profile, ok_pp = _opt_str(sp, "protection_profile", path, c)
    log_forwarding, ok_lf = _opt_str(sp, "log_forwarding", path, c)
    dos_profile, ok_dp = _opt_str(sp, "dos_profile", path, c)
    dos_log_forwarding, ok_dlf = _opt_str(sp, "dos_log_forwarding", path, c)
    user_id, ok_ui = _opt_bool(sp, "user_id", path, c)
    device_id, ok_di = _opt_bool(sp, "device_id", path, c)
    user_acl, ok_ua = _opt_acl(sp, "user_acl", path, c)
    device_acl, ok_da = _opt_acl(sp, "device_acl", path, c)

    # Reference names must exist on the firewall, same as a rule's profile /
    # log_forwarding (ADR-0003): a typo fails at PR time, not at device commit.
    _validate_name(protection_profile, c.zone_protection_catalog, f"{path}.protection_profile", c)
    _validate_name(log_forwarding, c.log_forwarding_catalog, f"{path}.log_forwarding", c)
    _validate_name(dos_log_forwarding, c.log_forwarding_catalog, f"{path}.dos_log_forwarding", c)

    if environment is None or zone is None or ztype is None or interfaces is None:
        return None
    if not all((ok_pp, ok_lf, ok_dp, ok_dlf, ok_ui, ok_di, ok_ua, ok_da)):
        return None
    return ZoneSpec(
        environment=environment, zone=zone, zone_type=ztype, interfaces=list(interfaces),
        protection_profile=protection_profile, log_forwarding=log_forwarding,
        user_id=user_id, device_id=device_id,
        dos_profile=dos_profile, dos_log_forwarding=dos_log_forwarding,
        user_acl=user_acl, device_acl=device_acl,
    )


#: Valid CIDR-ish shape for static addressing. Deliberately permissive on the
#: address itself — SCM reference-validates, and over-strict local regex would
#: reject forms the device accepts.
_CIDR_RE = re.compile(r"^[0-9a-fA-F:.]+/\d{1,3}$")


def _load_interface_spec(sp: Any, c: "_Collector") -> Optional[InterfaceSpec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None
    environment = _req_str(sp, "environment", path, c)
    interface = _req_str(sp, "interface", path, c)

    ip = sp.get("ip", [])
    if not isinstance(ip, list) or not all(isinstance(x, str) and x.strip() for x in ip):
        c.add(f"{path}.ip", "must be a list of CIDR strings, e.g. ['10.0.1.1/24']")
        ip = None
    elif ip and not all(_CIDR_RE.match(x.strip()) for x in ip):
        bad = [x for x in ip if not _CIDR_RE.match(x.strip())]
        c.add(f"{path}.ip", f"not CIDR form (address/prefix): {bad}")
        ip = None

    dhcp, ok_dhcp = _opt_bool(sp, "dhcp", path, c)
    mtu = sp.get("mtu")
    if mtu is not None and (not isinstance(mtu, int) or isinstance(mtu, bool) or mtu <= 0):
        c.add(f"{path}.mtu", f"must be a positive integer when set, got {mtu!r}")
        mtu = None
        ok_mtu = False
    else:
        ok_mtu = True
    comment, ok_comment = _opt_str(sp, "comment", path, c)
    management_profile, ok_mp = _opt_str(sp, "management_profile", path, c)
    _validate_name(management_profile, c.interface_profile_catalog,
                   f"{path}.management_profile", c)

    # The provider requires EXACTLY ONE of ip / dhcp_client / pppoe. Catch it
    # here rather than at the device commit, where the message is worse and the
    # change has already been applied to a candidate config.
    if ip is not None and ok_dhcp:
        if bool(ip) and dhcp:
            c.add(path, "set exactly one addressing mode: `ip` OR `dhcp`, not both")
            return None
        if not ip and not dhcp:
            c.add(path, "set an addressing mode: `ip: [...]` or `dhcp: true`")
            return None

    if environment is None or interface is None or ip is None:
        return None
    if not all((ok_dhcp, ok_mtu, ok_comment, ok_mp)):
        return None
    return InterfaceSpec(
        environment=environment, interface=interface,
        ip=[x.strip() for x in ip], dhcp=bool(dhcp), mtu=mtu,
        comment=comment, management_profile=management_profile,
    )


def _load_interface_request(data: dict, **catalogs: Any) -> InterfaceRequest:
    """Loader for `kind: InterfaceRequest` (kind #3)."""
    c = _Collector(**catalogs)
    if data.get("apiVersion") != API_VERSION:
        c.add("apiVersion", f"must be {API_VERSION!r}, got {data.get('apiVersion')!r}")
    metadata = _load_metadata(data.get("metadata"), c)
    spec = _load_interface_spec(data.get("spec"), c)
    if c.problems:
        raise IntentError(c.problems)
    return InterfaceRequest(metadata=metadata, spec=spec)  # type: ignore[arg-type]


def _load_zone_request(data: dict, **catalogs: Any) -> ZoneRequest:
    """Loader for `kind: ZoneRequest` (kind #2).

    Catalogs ARE used: a zone carries reference names (protection profile,
    log-forwarding) that must exist on the firewall, exactly like a rule's.
    """
    c = _Collector(**catalogs)
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
#: Intent kind -> loader. The ONE place a new kind's schema is registered; its
#: pipeline behaviour is registered once more in `fwgitops.kinds.REGISTRY`, and
#: a test asserts the two sets match exactly.
_KIND_LOADERS = {
    KIND: _load_access_request,
    ZONE_KIND: _load_zone_request,
    INTERFACE_KIND: _load_interface_request,
}


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

    # ADR-0003 optional rule components (all default to the "plain L4 allow" shape).
    application = _load_application(sp, path, c)
    profile, profile_ok = _opt_str(sp, "profile", path, c)
    _validate_name(profile, c.profile_catalog, f"{path}.profile", c)
    log_forwarding, logfwd_ok = _opt_str(sp, "log_forwarding", path, c)
    _validate_name(log_forwarding, c.log_forwarding_catalog, f"{path}.log_forwarding", c)
    position = _load_position(sp, path, c)

    # ── v1.0 rule-completeness fields ──
    description, desc_ok = _opt_str(sp, "description", path, c)
    log_start, ls_ok = _opt_bool(sp, "log_start", path, c)
    negate_source, ns_ok = _opt_bool(sp, "negate_source", path, c)
    negate_destination, nd_ok = _opt_bool(sp, "negate_destination", path, c)
    source_user = _opt_str_list(sp, "source_user", path, c)
    category = _opt_str_list(sp, "category", path, c)

    if environment is None or action not in _ACTIONS or source is None or destination is None \
            or service is None or not isinstance(log, bool) \
            or application is None or position is None or not profile_ok or not logfwd_ok \
            or not desc_ok or not ls_ok or not ns_ok or not nd_ok \
            or source_user is None or category is None:
        return None
    return Spec(
        environment=environment, action=action, source=source,
        destination=destination, service=service, log=log,
        application=application, profile=profile,
        log_forwarding=log_forwarding, position=position,
        description=description, log_start=bool(log_start),
        source_user=source_user, category=category,
        negate_source=bool(negate_source), negate_destination=bool(negate_destination),
    )


def _opt_bool(obj: dict, key: str, path: str, c: _Collector):
    """Optional boolean: (value, ok). Absent -> (False, True)."""
    if key not in obj:
        return False, True
    val = obj.get(key)
    if not isinstance(val, bool):
        c.add(f"{path}.{key}", f"must be a boolean, got {type(val).__name__}")
        return None, False
    return val, True


def _opt_str_list(sp: dict, key: str, path: str, c: _Collector) -> Optional[List[str]]:
    """Optional list of non-empty strings (e.g. source_user, category). Omitted -> ['any']."""
    raw = sp.get(key)
    if raw is None:
        return ["any"]
    if not isinstance(raw, list) or not raw or not all(
        isinstance(x, str) and x.strip() for x in raw
    ):
        c.add(f"{path}.{key}", "must be a non-empty list of strings")
        return None
    return [x.strip() for x in raw]


def _opt_str(obj: dict, key: str, path: str, c: _Collector):
    """An optional string field: (value|None, ok). None+ok means 'omitted'."""
    val = obj.get(key)
    if val is None:
        return None, True
    if not isinstance(val, str) or not val.strip():
        c.add(f"{path}.{key}", "must be a non-empty string when set")
        return None, False
    return val.strip(), True


def _opt_bool(obj: dict, key: str, path: str, c: _Collector):
    """An optional boolean field: (value|None, ok). None+ok means 'omitted'."""
    val = obj.get(key)
    if val is None:
        return None, True
    if not isinstance(val, bool):
        c.add(f"{path}.{key}", f"must be true or false when set, got {val!r}")
        return None, False
    return val, True


def _opt_acl(obj: dict, key: str, path: str, c: _Collector):
    """An optional {include: [...], exclude: [...]} ACL: (ZoneAcl|None, ok)."""
    raw = obj.get(key)
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        c.add(f"{path}.{key}", "must be a mapping with `include` and/or `exclude` lists")
        return None, False
    unknown = sorted(set(raw) - {"include", "exclude"})
    if unknown:
        c.add(f"{path}.{key}", f"unknown field(s) {unknown}; expected `include` / `exclude`")
        return None, False
    out = {}
    for side in ("include", "exclude"):
        vals = raw.get(side, [])
        if not isinstance(vals, list) or not all(isinstance(x, str) and x.strip() for x in vals):
            c.add(f"{path}.{key}.{side}", "must be a list of non-empty strings")
            return None, False
        out[side] = [x.strip() for x in vals]
    return ZoneAcl(include=out["include"], exclude=out["exclude"]), True


def _validate_name(value: Optional[str], catalog: Any, path: str, c: _Collector) -> None:
    """If a NameCatalog is configured, the reference name must be known (ADR-0003).

    No catalog -> no check (the name is accepted free-form; back-compat). Omitted
    value (None) is always fine — the field is optional.
    """
    if value is None or catalog is None:
        return
    from fwgitops.catalog import CatalogError  # local import: no hard dep in Phase 1
    try:
        catalog.validate(value)
    except CatalogError as e:
        c.add(path, str(e))


def _load_application(sp: dict, path: str, c: _Collector) -> Optional[List[str]]:
    """App-ID list. Omitted -> ['any']; else a non-empty list of App-ID names.

    When an application catalog is configured, every name (except the built-in
    `any`) must be known — a typo'd App-ID fails here, not at the device commit.
    """
    raw = sp.get("application")
    if raw is None:
        return ["any"]
    if not isinstance(raw, list) or not raw or not all(
        isinstance(a, str) and a.strip() for a in raw
    ):
        c.add(f"{path}.application", "must be a non-empty list of App-ID names (strings)")
        return None
    apps = [a.strip() for a in raw]
    for a in apps:
        _validate_name(a, c.application_catalog, f"{path}.application", c)
    return apps


def _load_position(sp: dict, path: str, c: _Collector) -> Optional[str]:
    """Ordering directive: top | bottom | before:<rule> | after:<rule>."""
    val = sp.get("position", "bottom")
    if isinstance(val, str) and val.strip():
        v = val.strip()
        if v in ("top", "bottom"):
            return v
        rel, sep, tgt = v.partition(":")
        if sep and rel in ("before", "after") and tgt.strip():
            return f"{rel}:{tgt.strip()}"
    c.add(f"{path}.position",
          f"invalid position {val!r}; use top | bottom | before:<rule> | after:<rule>")
    return None


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
