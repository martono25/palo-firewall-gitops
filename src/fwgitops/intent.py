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
#: Port-based protocols — these compile to an `scm_service` OBJECT.
_PORT_PROTOCOLS = {"tcp", "udp"}
#: ICMP has no ports, so it cannot be an `scm_service` at all (the resource
#: requires one). PAN-OS matches it by APPLICATION instead, and SCM accepts
#: `application: [ping]` with `service: [application-default]` — MEASURED in
#: `spike/icmp-service-shape`, along with the fact that omitting `service`
#: entirely is REJECTED (400, `"service" is required`) even though the provider
#: schema marks it optional.
_APPLICATION_PROTOCOLS = {"icmp"}
_PROTOCOLS = _PORT_PROTOCOLS | _APPLICATION_PROTOCOLS
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
    protocol: str            # "tcp" | "udp" | "icmp"
    #: None for a protocol that has no ports (icmp). Optional rather than "" so a
    #: missing port cannot be confused with an empty one.
    port: Optional[str] = None

    @property
    def is_application_matched(self) -> bool:
        """True when PAN-OS matches this by APPLICATION rather than by port."""
        return self.protocol in _APPLICATION_PROTOCOLS


#: Every key each kind's `spec:` may carry. One frozenset per loader, checked at
#: the END of that loader so field-level problems are reported first — being told
#: `logging` is unknown is more useful alongside "and `log` must be a bool" than
#: instead of it.
#:
#: WHY EXPLICIT rather than derived at runtime: the loaders read keys through
#: several helpers, some of which read `sp` themselves, so a runtime derivation
#: would be a second implementation of the thing it validates. Instead
#: `tests/test_intent.py` walks the AST of each loader — following helpers that
#: take `sp` — and asserts these sets EXACTLY match the keys actually read. A key
#: the loader reads but that is missing here would reject a valid intent; one
#: listed here but never read is a dead allowance that lets a typo through.
_ACCESS_SPEC_KEYS = frozenset({
    "environment", "action", "source", "destination", "service", "log",
    "application", "profile", "log_forwarding", "position", "description",
    "log_start", "source_user", "category", "negate_source", "negate_destination",
})
_ZONE_SPEC_KEYS = frozenset({
    "folder", "device", "environment", "zone", "type", "interfaces",
    "protection_profile", "log_forwarding", "dos_profile", "dos_log_forwarding",
    "user_id", "device_id", "user_acl", "device_acl",
})
_INTERFACE_SPEC_KEYS = frozenset({
    "folder", "device", "environment", "interface", "ip", "dhcp", "mtu",
    "comment", "management_profile",
})
_ROUTE_SPEC_KEYS = frozenset({
    "folder", "device", "environment", "destination", "nexthop",
    "nexthop_interface", "router", "vrf", "metric", "admin_dist",
})


def _reject_unknown(sp: Any, allowed: "frozenset[str]", path: str, c: "_Collector") -> None:
    """Unknown `spec:` keys are REJECTED, not ignored.

    This is the sharper half of the metadata guard. `metadata:` is paperwork; a
    dropped key there costs an audit trail. `spec:` is FIREWALL BEHAVIOUR, so a
    dropped key is a rule that does not do what it says and looks fine doing it:

        spec:
          logging: true      # compiles clean, logs nothing — the field is `log`

    No plan diff, no warning, no failed apply. The rule is simply weaker than
    the request that was approved.
    """
    if not isinstance(sp, dict):
        return
    unknown = sorted(str(k) for k in set(sp) - allowed)
    if unknown:
        c.add(path, f"unknown field(s) {unknown}; expected {sorted(allowed)}. "
                    f"Unknown keys are rejected rather than ignored — in `spec` a "
                    f"silently dropped field is a rule that does not do what it says.")


#: Every key `metadata:` may carry. Mirrors `Metadata` exactly; a test asserts
#: the two cannot drift apart, because a field added to the dataclass and not
#: here would be rejected in every intent that used it.
_METADATA_KEYS = frozenset({"id", "requester", "ticket", "justification", "requested"})


@dataclass(frozen=True)
class Metadata:
    id: str
    requester: str
    ticket: str
    justification: str
    requested: date


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
    zone: str
    zone_type: str          # layer3 | layer2 | virtual-wire | tap | external | tunnel
    interfaces: List[str]    # member interfaces (may be empty — an empty zone is valid)

    # ── Target (exactly one) ──────────────────────────────────────────────
    # The author of a Day-1 change is a network engineer for whom the target IS
    # the intent, so it is named directly:
    #   `folder` — an SCM folder
    #   `device` — a FIREWALL serial. In SCM the firewall is the last level of
    #              the hierarchy and inherits down it, but it is addressed
    #              `device=`, never `folder=`. The narrower target: a
    #              device-scope write creates a per-device override
    #              (spike/device-override-probe).
    # `environment` is the app-language indirection AccessRequest uses.
    # Validated in `_load_target`: declared AND targetable, or rejected.
    folder: Optional[str] = None
    device: Optional[str] = None
    environment: Optional[str] = None

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
class RouteSpec:
    """kind: RouteRequest (ADR-0001 kind #4) — ONE static route.

    Routes live four levels inside a logical router
    (`vrf[].routing_table.ip.static_route[]`), and that same router object also
    records which interfaces belong to the VRF. Terraform manages whole objects,
    so the compiler AGGREGATES every route for a router into one resource.

    `vrf_interfaces` is resolved from `catalog/routers.yaml` at load time and
    carried here, so the compiler stays pure and the aggregated router is never
    written without its interface membership — which would break the object all
    traffic depends on.
    """

    destination: str                     # CIDR, e.g. "0.0.0.0/0"

    # ── Target (exactly one) ──────────────────────────────────────────────
    # The author of a Day-1 change is a network engineer for whom the target IS
    # the intent, so it is named directly:
    #   `folder` — an SCM folder
    #   `device` — a FIREWALL serial. In SCM the firewall is the last level of
    #              the hierarchy and inherits down it, but it is addressed
    #              `device=`, never `folder=`. The narrower target: a
    #              device-scope write creates a per-device override
    #              (spike/device-override-probe).
    # `environment` is the app-language indirection AccessRequest uses.
    # Validated in `_load_target`: declared AND targetable, or rejected.
    folder: Optional[str] = None
    device: Optional[str] = None
    environment: Optional[str] = None
    router: str = "default"
    vrf: str = "default"
    #: Next-hop IP. Mutually exclusive with `nexthop_interface`.
    nexthop: Optional[str] = None
    #: Next-hop interface (for point-to-point / DHCP-learned links).
    nexthop_interface: Optional[str] = None
    metric: Optional[int] = None
    admin_dist: Optional[int] = None
    #: Resolved from the router catalog at load time — NOT requester input.
    vrf_interfaces: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteRequest:
    """kind: RouteRequest — one static route in a folder's logical router."""

    metadata: Metadata
    spec: RouteSpec


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

    interface: str                       # the folder-scope name, e.g. "$eth-local"

    # ── Target (exactly one) ──────────────────────────────────────────────
    # The author of a Day-1 change is a network engineer for whom the target IS
    # the intent, so it is named directly:
    #   `folder` — an SCM folder
    #   `device` — a FIREWALL serial. In SCM the firewall is the last level of
    #              the hierarchy and inherits down it, but it is addressed
    #              `device=`, never `folder=`. The narrower target: a
    #              device-scope write creates a per-device override
    #              (spike/device-override-probe).
    # `environment` is the app-language indirection AccessRequest uses.
    # Validated in `_load_target`: declared AND targetable, or rejected.
    folder: Optional[str] = None
    device: Optional[str] = None
    environment: Optional[str] = None
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
        router_catalog: Any = None,
        env_map: Any = None,
        folder_hierarchy: Any = None,
        interface_catalog: Any = None,
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
        #: Router/VRF topology (RouteRequest) + the env map needed to resolve an
        #: environment to its folder before looking membership up.
        self.router_catalog = router_catalog
        self.env_map = env_map
        #: FolderHierarchy — validates a Day-1 kind's explicit `folder:`.
        #: Its ABSENCE makes `folder:` unusable (fail closed), not unchecked.
        self.folder_hierarchy = folder_hierarchy
        #: InterfaceCatalog — resolves an interface ROLE to the object name at
        #: the target scope. `$eth-local` at folder scope, `ethernet1/4` at
        #: device scope: one object, two names (ADR-0005).
        self.interface_catalog = interface_catalog

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
    router_catalog: Any = None,
    env_map: Any = None,
    folder_hierarchy: Any = None,
    interface_catalog: Any = None,
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
        router_catalog=router_catalog, env_map=env_map,
        folder_hierarchy=folder_hierarchy,
        interface_catalog=interface_catalog,
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
ROUTE_KIND = "RouteRequest"


def _load_zone_spec(sp: Any, c: _Collector) -> Optional[ZoneSpec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None
    _reject_unknown(sp, _ZONE_SPEC_KEYS, path, c)
    folder, device, environment = _load_target(sp, path, c)
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

    if (folder is None and device is None and environment is None) or zone is None \
            or ztype is None or interfaces is None:
        return None
    if not all((ok_pp, ok_lf, ok_dp, ok_dlf, ok_ui, ok_di, ok_ua, ok_da)):
        return None
    return ZoneSpec(
        folder=folder, device=device, environment=environment,
        zone=zone, zone_type=ztype, interfaces=list(interfaces),
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
    _reject_unknown(sp, _INTERFACE_SPEC_KEYS, path, c)
    folder, device, environment = _load_target(sp, path, c)
    role = _req_str(sp, "interface", path, c)

    # `interface:` names a ROLE, not a port. The same interface is `$eth-local`
    # at folder scope and `ethernet1/4` at device scope (ADR-0005), so which
    # literal is correct depends on the target — and the physical name is a
    # property of the AWS topology, which has changed once already. Resolving
    # here keeps the compiler pure, exactly as the router catalog does for VRF
    # membership.
    interface = None
    if role is not None:
        if c.interface_catalog is None:
            # Fail closed: with no catalog there is nothing to resolve against,
            # and guessing a port is how an intent lands on the wrong wire.
            c.add(f"{path}.interface",
                  "cannot resolve `interface` — no interface catalog loaded "
                  "(catalog/interfaces.yaml). Refusing to guess a physical port.")
        else:
            target_device = device
            if target_device is None and environment is None and folder is None:
                target_device = None      # target already reported by _load_target
            try:
                interface = c.interface_catalog.resolve(role, device=target_device)
            except Exception as e:  # CatalogError — reported as an intent problem
                c.add(f"{path}.interface", str(e))

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

    if (folder is None and device is None and environment is None) \
            or interface is None or ip is None:
        return None
    if not all((ok_dhcp, ok_mtu, ok_comment, ok_mp)):
        return None
    return InterfaceSpec(
        folder=folder, device=device, environment=environment, interface=interface,
        ip=[x.strip() for x in ip], dhcp=bool(dhcp), mtu=mtu,
        comment=comment, management_profile=management_profile,
    )


def _load_target(
    sp: dict, path: str, c: "_Collector",
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve a Day-1 kind's target. Returns `(folder, device, environment)`.

    Exactly one of `folder:` / `device:` / `environment:`.

    WHY THREE. `AccessRequest` is authored by app teams who should never need to
    know SCM topology, so it keeps `environment:` and the platform maps it to a
    folder. The Day-1 kinds are authored by network engineers, for whom the
    target IS the intent. Forcing one addressing model on both would be the faked
    uniformity ADR-0001's registry exists to avoid.

    FOLDER vs DEVICE. In SCM the FIREWALL IS THE LAST LEVEL of the hierarchy and
    inherits down it (All -> ngfw-shared -> prod-edge -> firewall) — but it is
    addressed `device=<serial>`, never `folder=<serial>`, which returns 400
    "Folder doesn't exist". They are separate fields here because they are
    separate scopes, not because a firewall sits outside the hierarchy.

    Targeting a firewall is the NARROWER act: verified in
    `spike/device-override-probe`, a device-scope write to an inherited object
    creates a per-device override, leaving the shared object, the other firewall
    and the parent folders untouched.

    Both `folder:` and `device:` are only safe because they are checked against
    the catalog here: unknown or non-targetable is REJECTED, not tiered up. HIGH
    is approvable, and a write to a shared parent like `ngfw-shared` should not
    be one rubber-stamp away from reaching every firewall at once.

    Targetability is deliberately NOT applied to the `environment:` path — that
    mapping lives in `catalog/environments.yaml`, which is reviewed platform
    config, not requester input. The threat model here is the field a requester
    writes.
    """
    folder, ok_f = _opt_str(sp, "folder", path, c)
    device, ok_d = _opt_str(sp, "device", path, c)
    environment, ok_e = _opt_str(sp, "environment", path, c)
    if not (ok_f and ok_d and ok_e):
        return None, None, None

    given = [n for n, v in (("folder", folder), ("device", device),
                            ("environment", environment)) if v]
    if len(given) > 1:
        c.add(path, f"set exactly one target, got {given}: `folder` (SCM folder), "
                    f"`device` (firewall serial) or `environment`")
        return None, None, None
    if not given:
        c.add(path, "set a target: `folder: <scm-folder>`, `device: <serial>` "
                    "or `environment: <name>`")
        return None, None, None

    h = c.folder_hierarchy
    if (folder or device) and h is None:
        # Fail closed. Without the catalog there is nothing to check against,
        # and an unchecked target is the whole footgun.
        field_name = "folder" if folder else "device"
        c.add(f"{path}.{field_name}",
              f"cannot validate `{field_name}` — no folder catalog loaded "
              f"(catalog/folders.yaml). Refusing to target an unchecked scope.")
        return None, None, None

    if folder:
        if h.device_known(folder):
            # The v1.11.0 mistake, caught at the requester's door.
            c.add(f"{path}.folder",
                  f"{folder!r} is a FIREWALL, not a folder — `folder={folder}` returns "
                  f"400 'Folder doesn\'t exist' from SCM. Use `device: {folder}` instead.")
            return None, None, None
        if not h.known(folder):
            c.add(f"{path}.folder",
                  f"folder {folder!r} is not declared in catalog/folders.yaml. "
                  f"Declare it there (in the same PR that onboards it) before targeting it.")
            return None, None, None
        if not h.is_targetable(folder):
            kids = ", ".join(sorted(h.children_of(folder)))
            why = (f" — it is a parent of [{kids}], so a change here reaches all of them"
                   if kids else "")
            c.add(f"{path}.folder",
                  f"folder {folder!r} is not targetable{why}. "
                  f"Targetable folders: {', '.join(h.targetable_folders()) or '(none)'}.")
            return None, None, None


    if device:
        if h.known(device):
            c.add(f"{path}.device",
                  f"{device!r} is a folder, not a firewall. Use `folder: {device}`.")
            return None, None, None
        if not h.device_known(device):
            c.add(f"{path}.device",
                  f"firewall {device!r} is not declared in catalog/folders.yaml. "
                  f"Declare it under its folder's `devices:` (in the same PR that "
                  f"onboards it) before targeting it.")
            return None, None, None
        if not h.is_device_targetable(device):
            c.add(f"{path}.device",
                  f"firewall {device!r} is not targetable. Targetable firewalls: "
                  f"{', '.join(h.targetable_device_serials()) or '(none)'}.")
            return None, None, None

    return folder, device, environment


def _resolve_target_scope(
    folder: Optional[str], device: Optional[str], environment: Optional[str],
    c: "_Collector",
) -> Optional[str]:
    """The catalog KEY a Day-1 intent lands under, however it was addressed.

    A firewall keys as `device:<serial>` so catalogs (routers.yaml) can hold a
    per-firewall entry distinct from its folder's, matching the override
    semantics SCM actually has.

    Load-time resolution, so catalogs keyed by folder (routers.yaml) can be
    consulted here and the compiler stays pure. Returns None when the target is
    not yet knowable — the missing-target problem is reported by `_load_target`,
    so this stays quiet rather than double-reporting.
    """
    if folder is not None:
        return folder
    if device is not None:
        return f"device:{device}"
    if environment is not None and c.env_map is not None:
        try:
            return c.env_map.resolve(environment).folder
        except Exception:  # noqa: BLE001 - env resolution is reported elsewhere
            return None
    return None


def _load_route_spec(sp: Any, c: "_Collector") -> Optional[RouteSpec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None
    _reject_unknown(sp, _ROUTE_SPEC_KEYS, path, c)
    folder, device, environment = _load_target(sp, path, c)
    destination = _req_str(sp, "destination", path, c)
    if destination is not None and not _CIDR_RE.match(destination):
        c.add(f"{path}.destination", f"must be CIDR (address/prefix), got {destination!r}")
        destination = None

    router, ok_r = _opt_str(sp, "router", path, c)
    vrf, ok_v = _opt_str(sp, "vrf", path, c)
    router = router or "default"
    vrf = vrf or "default"

    nexthop, ok_nh = _opt_str(sp, "nexthop", path, c)
    nexthop_interface, ok_nhi = _opt_str(sp, "nexthop_interface", path, c)
    if ok_nh and ok_nhi:
        if nexthop and nexthop_interface:
            c.add(path, "set exactly one next hop: `nexthop` (IP) OR `nexthop_interface`")
            return None
        if not nexthop and not nexthop_interface:
            c.add(path, "set a next hop: `nexthop: <ip>` or `nexthop_interface: <name>`")
            return None
    if nexthop is not None:
        try:
            ipaddress.ip_address(nexthop)
        except ValueError:
            c.add(f"{path}.nexthop", f"must be an IP address, got {nexthop!r}")
            return None

    metric, ok_m = _opt_positive_int(sp, "metric", path, c)
    admin_dist, ok_a = _opt_positive_int(sp, "admin_dist", path, c)

    # VRF membership comes from the platform catalog, never from the requester:
    # the compiler aggregates routes into one router object, and writing that
    # object without its interface list would break routing wholesale.
    interfaces: Tuple[str, ...] = ()
    target = _resolve_target_scope(folder, device, environment, c)
    if target is not None and c.router_catalog is not None:
        found = c.router_catalog.interfaces_for(target, router, vrf)
        if found is None:
            known = ", ".join(c.router_catalog.known(target)) or "(none declared)"
            c.add(f"{path}.router",
                  f"router/vrf {router}/{vrf} is not declared for folder {target!r} in "
                  f"catalog/routers.yaml; known: {known}")
            return None
        interfaces = found

    if (folder is None and device is None and environment is None) or destination is None:
        return None
    if not all((ok_r, ok_v, ok_nh, ok_nhi, ok_m, ok_a)):
        return None
    return RouteSpec(
        folder=folder, device=device, environment=environment,
        destination=destination, router=router, vrf=vrf,
        nexthop=nexthop, nexthop_interface=nexthop_interface,
        metric=metric, admin_dist=admin_dist, vrf_interfaces=interfaces,
    )


def _load_route_request(data: dict, **catalogs: Any) -> RouteRequest:
    """Loader for `kind: RouteRequest` (kind #4)."""
    c = _Collector(**catalogs)
    if data.get("apiVersion") != API_VERSION:
        c.add("apiVersion", f"must be {API_VERSION!r}, got {data.get('apiVersion')!r}")
    metadata = _load_metadata(data.get("metadata"), c)
    spec = _load_route_spec(data.get("spec"), c)
    if c.problems:
        raise IntentError(c.problems)
    return RouteRequest(metadata=metadata, spec=spec)  # type: ignore[arg-type]


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
    ROUTE_KIND: _load_route_request,
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

    # REMOVED 2026-08-05, and REJECTED rather than ignored. Unknown metadata
    # keys are silently dropped by this loader, so deleting the field alone
    # would turn every existing `expires:` into a silent no-op — the exact
    # "compiles clean, does nothing" failure this codebase treats as a bug.
    #
    # It modelled a lifecycle this platform does not run: nothing removed an
    # expired rule, and the date was never enforced anywhere. On a Day-1 kind it
    # was parsed and dropped entirely, since evidence bundles are AccessRequest
    # -only. A field that means nothing is worse than a missing one, because a
    # reader assumes it means something.
    if isinstance(md, dict) and "expires" in md:
        c.add(f"{path}.expires",
              "removed — this platform does not model rule expiry. Nothing enforced "
              "the date: it was never applied to the firewall, and no job removed an "
              "expired rule. Delete the field. For expiry the DEVICE enforces, see "
              "`scm_security_rule.schedule` (PAN-OS schedules), which is a separate "
              "capability this platform does not yet wire.")

    # UNKNOWN KEYS ARE REJECTED, not ignored.
    #
    # Silently dropping them is how a field stops working without anyone
    # noticing: a typo (`justifcation:`) reads as "the required field is
    # missing", which at least fails — but `tickets:` or a retired field reads as
    # accepted and does nothing. The `expires` retirement made that concrete;
    # this closes the class rather than the instance.
    #
    # `expires` is excluded here because it has its own message above explaining
    # what replaced it. Folding it into a generic "unknown key" list would throw
    # that away exactly when someone needs it.
    if isinstance(md, dict):
        unknown = sorted(str(k) for k in set(md) - _METADATA_KEYS - {"expires"})
        if unknown:
            c.add(path, f"unknown field(s) {unknown}; expected "
                        f"{sorted(_METADATA_KEYS)}. Unknown keys are rejected rather "
                        f"than ignored — a silently dropped field looks exactly like "
                        f"one that works.")

    if None in (_id, requester, ticket, justification, requested):
        return None
    return Metadata(
        id=_id, requester=requester, ticket=ticket,  # type: ignore[arg-type]
        justification=justification, requested=requested,  # type: ignore[arg-type]
    )


def _load_spec(sp: Any, c: _Collector) -> Optional[Spec]:
    path = "spec"
    if not isinstance(sp, dict):
        c.add(path, "required mapping")
        return None

    # `folder`/`device` are excluded from the generic sweep: they were just
    # reported above with the reason. Reporting them twice buries the specific
    # message under "unknown field(s)", which is the one that actually answers
    # the author's question. Same treatment `expires` gets in _load_metadata.
    _reject_unknown(sp, _ACCESS_SPEC_KEYS | {"folder", "device"}, path, c)

    environment = _req_str(sp, "environment", path, c)
    # TARGETING IS A DECISION, NOT AN OVERSIGHT (ADR-0007).
    #
    # `folder:` is meaningful vocabulary on the Day-1 kinds, so it WILL get
    # copied into an AccessRequest. Ignoring it silently would land the rule in
    # whatever `environment` resolves to while the author believes they targeted
    # the folder they named — a silently wrong target, which is the exact failure
    # class this platform exists to prevent.
    #
    # SCM DOES accept a security rule at device scope (verified 2026-08-05, after
    # a re-onboard fixed the broken registration that made three earlier spikes
    # conclude otherwise). So this is a choice, and each key says why — a generic
    # "unknown field" would tell the author the field is wrong and nothing about
    # why, and for a TARGET field why is the whole question.
    for key, why in (
        ("folder", "an app team should not need to know SCM topology, and an intent "
                   "naming a folder breaks when that folder is renamed or its firewall "
                   "moves — `environment` absorbed two topology events in one week "
                   "without a single intent changing"),
        ("device", "a per-firewall rule is a policy OVERRIDE, and policy that differs "
                   "per firewall is divergence someone reasons about for as long as it "
                   "exists. Device scope is for CONFIGURATION — an interface address is "
                   "genuinely per-firewall — but the unit of POLICY is the folder"),
    ):
        if key in sp:
            c.add(f"{path}.{key}",
                  f"an AccessRequest targets an `environment:`, never a {key}: {why}. "
                  f"See ADR-0007. If a rule genuinely must apply to one firewall, give "
                  f"that firewall its own folder and environment.")

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


def _opt_positive_int(obj: dict, key: str, path: str, c: _Collector):
    """An optional positive integer: (value|None, ok)."""
    val = obj.get(key)
    if val is None:
        return None, True
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        c.add(f"{path}.{key}", f"must be a positive integer when set, got {val!r}")
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
    if not ok:
        return None

    # NO MIXING application-matched and port-based services in one request.
    # `service` is a RULE-LEVEL list, so an ICMP entry forces the whole rule to
    # `application-default` — which would silently re-interpret the tcp/udp
    # entries beside it as "their App-ID's default ports" rather than the ports
    # actually requested. Two requests, two rules, each meaning what it says.
    app = sorted({x.protocol for x in out if x.is_application_matched})
    port = sorted({x.protocol for x in out if not x.is_application_matched})
    if app and port:
        c.add(path,
              f"cannot mix {app} with {port} in one request. `service` is a "
              f"rule-level list and {app[0]!r} forces the rule to "
              f"`application-default`, which would silently change what the "
              f"{port} entries match. File them as separate requests.")
        return None
    return out


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
        return None

    if protocol in _APPLICATION_PROTOCOLS:
        # A `port` alongside `icmp` is REJECTED, not ignored. ICMP has no ports,
        # so accepting one would let a requester write a number that reads like a
        # restriction and enforces nothing — the same silently-dropped-field trap
        # that `_reject_unknown` exists to close.
        if "port" in item:
            c.add(f"{path}.port",
                  f"{protocol!r} has no ports, so `port` cannot restrict anything here. "
                  f"Remove it — a value that looks like a restriction and enforces "
                  f"nothing is worse than no value.")
            return None
        return Service(protocol=protocol, port=None)

    port_str = _validate_port(item.get("port"), f"{path}.port", c)
    if port_str is None:
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
