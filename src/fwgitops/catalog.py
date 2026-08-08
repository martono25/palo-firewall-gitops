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
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

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
    """A named application: its environment and zone, and where it lives.

    NO `folder` (removed 2026-08-05, "Model A"). A rule's folder is a property of
    the TRAFFIC PATH, not of either endpoint: a rule from an app in folder X to
    one in folder Y traverses both firewalls, so asking an app which folder to
    use is ambiguous for most rules. The folder comes from `environment`, and an
    app's folder is therefore derivable from its environment rather than declared
    beside it.

    It had been declared in catalog/apps.yaml and stored here, and the compiler
    NEVER READ IT — `_target()` has always used `env_map.resolve(...).folder`. An
    app whose folder contradicted its environment loaded without complaint and
    the contradiction did nothing, which is the silent-drop failure this codebase
    removes wherever it appears.
    """

    environment: str
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
            raise CatalogError("'apps' must be a mapping of name -> {environment, zone, ...}")

        out: Dict[str, AppDef] = {}
        problems: List[str] = []
        for name, spec in entries.items():
            if not isinstance(spec, dict):
                problems.append(f"{name}: must be a mapping")
                continue
            # REJECTED, not ignored. This loader would otherwise drop `folder:`
            # silently — which is exactly what it did until 2026-08-05, so every
            # shipped app declared one that had no effect. Deleting the field
            # without rejecting it would leave those files looking correct.
            if "folder" in spec:
                problems.append(
                    f"{name}.folder: removed — an app does not choose the folder. A rule's "
                    f"folder comes from its `environment` (catalog/environments.yaml), because "
                    f"the folder is a property of the traffic path, not of an endpoint. Delete "
                    f"this line; the app's folder is its environment's folder.")
            env, zone = spec.get("environment"), spec.get("zone")
            strs_ok = True
            for field, val in (("environment", env), ("zone", zone)):
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
                    environment=env, zone=zone,
                    addresses=tuple(valid_addr), fqdns=tuple(str(f) for f in fqdns),
                )
        if problems:
            raise CatalogError("invalid app catalog:\n  - " + "\n  - ".join(problems))
        return cls(apps=out)


@dataclass(frozen=True)
class FolderHierarchy:
    """Which SCM folders have children (validation-only, no expansion).

    A change scoped to a folder with children reaches every descendant. That is
    the largest blast radius this platform can produce, so the classifier tiers
    it up — see `classify`'s `folder_with_children` check and ADR-0005.

    Absent hierarchy means no check, never a silent pass to LOW: the caller
    decides whether to require one.

    Also records TARGETABILITY. The Day-1 kinds name `folder:` directly in the
    intent (their author is a network engineer; `environment:` resolves 1:1 and
    cannot express a device folder), so something has to stop that field naming
    `ngfw-shared` and reaching every device at once. `targetable` is that stop,
    and it rejects at compile time rather than merely tiering the change up —
    HIGH is approvable, and a shared-parent write should not be one rubber-stamp
    away.
    """

    children: Dict[str, FrozenSet[str]]
    #: Folders explicitly marked targetable. A folder absent from this set is
    #: NOT targetable — see `is_targetable`.
    targetable: FrozenSet[str] = frozenset()
    #: serial -> the folder it sits under. In SCM the FIREWALL IS THE LAST LEVEL
    #: of the hierarchy and inherits down it, so firewalls are modelled here —
    #: but they are addressed `device=<serial>`, never `folder=<serial>`, so
    #: they are kept apart from `children`.
    devices: Dict[str, str] = field(default_factory=dict)
    #: Serials explicitly marked targetable, same fail-closed rule as folders.
    targetable_devices: FrozenSet[str] = frozenset()
    #: serial -> the DISPLAY NAME this platform expects SCM to show.
    #:
    #: Not the firewall's hostname — that is `ip-10-100-0-51` here, set by DHCP,
    #: and nothing declares it. This is the label SCM shows in its inventory,
    #: which a re-onboard RESETS (it went to `PA-VM` on 2026-08-05 and nothing
    #: noticed). Declared so `verify-catalog` can catch that: the reset itself is
    #: cosmetic, but it is a reliable symptom of a re-registration, and a
    #: re-registration wipes device-scope config.
    device_display_names: Dict[str, str] = field(default_factory=dict)

    def has_children(self, folder: str) -> bool:
        return bool(self.children.get(folder))

    def children_of(self, folder: str) -> FrozenSet[str]:
        return self.children.get(folder, frozenset())

    def known(self, folder: str) -> bool:
        return folder in self.children

    def is_targetable(self, folder: str) -> bool:
        """Fail closed: unknown folders and undeclared ones are NOT targetable.

        An unknown folder is the dangerous case — a typo'd or newly created
        folder must not inherit permission by default. Declaring
        `targetable: true` is the only way in.
        """
        return folder in self.targetable

    def targetable_folders(self) -> List[str]:
        """For error messages — tells the requester what they may actually name."""
        return sorted(self.targetable)

    # ── Firewalls (the last level of the hierarchy) ───────────────────────
    def device_known(self, serial: str) -> bool:
        return serial in self.devices

    def is_device_targetable(self, serial: str) -> bool:
        """Fail closed, exactly as for folders: undeclared == not targetable."""
        return serial in self.targetable_devices

    def targetable_device_serials(self) -> List[str]:
        return sorted(self.targetable_devices)

    def folder_of_device(self, serial: str) -> Optional[str]:
        """The folder a firewall sits under — what it inherits from."""
        return self.devices.get(serial)

    def devices_of(self, folder: str) -> FrozenSet[str]:
        """Firewalls beneath a folder. A change to the folder reaches them all."""
        return frozenset(s for s, f in self.devices.items() if f == folder)

    @classmethod
    def from_dict(cls, data: Any) -> "FolderHierarchy":
        """Build from parsed YAML. Fails closed on a bad shape."""
        if not isinstance(data, dict):
            raise CatalogError("folder hierarchy must be a mapping")
        folders = data.get("folders", data)
        if not isinstance(folders, dict):
            raise CatalogError("folder hierarchy: `folders` must be a mapping")
        out: Dict[str, FrozenSet[str]] = {}
        targetable: set = set()
        devices: Dict[str, str] = {}
        display_names: Dict[str, str] = {}
        targetable_devices: set = set()
        for name, spec in folders.items():
            # A device folder is named for the serial. Unquoted, YAML reads a
            # serial with NO leading zero (123456789012345) as an int — the
            # tenant's current two happen to start with `00`, which YAML rejects
            # as octal and falls back to str, so they survive by luck rather than
            # design. Coercing an int back to str here would be worse than
            # rejecting: `1.23e+14` and friends would never match a real folder.
            if isinstance(name, int):
                raise CatalogError(
                    f"folder hierarchy: folder name {name!r} parsed as a number — quote it "
                    f'("{name}"). Device folders are named for the serial and leading zeros '
                    f"are significant."
                )
            if not isinstance(name, str) or not name.strip():
                raise CatalogError(f"folder hierarchy: bad folder name {name!r}")
            kids = spec.get("children", []) if isinstance(spec, dict) else spec
            if kids is None:
                kids = []
            if not isinstance(kids, list) or not all(
                isinstance(k, str) and k.strip() for k in kids
            ):
                raise CatalogError(
                    f"folder hierarchy: {name!r}.children must be a list of folder names"
                )
            out[name.strip()] = frozenset(k.strip() for k in kids)

            flag = spec.get("targetable") if isinstance(spec, dict) else None
            if flag is not None and not isinstance(flag, bool):
                raise CatalogError(
                    f"folder hierarchy: {name!r}.targetable must be true or false, "
                    f"got {flag!r}"
                )
            if flag is True:
                targetable.add(name.strip())

            # Firewalls beneath this folder. Same quoting trap as folder names:
            # a serial with no leading zero parses as an int.
            devs = spec.get("devices") if isinstance(spec, dict) else None
            if devs is None:
                continue
            if not isinstance(devs, dict):
                raise CatalogError(
                    f"folder hierarchy: {name!r}.devices must be a mapping of "
                    f"serial -> spec")
            for serial, dspec in devs.items():
                if isinstance(serial, int):
                    raise CatalogError(
                        f"folder hierarchy: device serial {serial!r} parsed as a number — "
                        f'quote it ("{serial}").')
                if not isinstance(serial, str) or not serial.strip():
                    raise CatalogError(f"folder hierarchy: bad device serial {serial!r}")
                serial = serial.strip()
                if serial in devices:
                    raise CatalogError(
                        f"folder hierarchy: device {serial!r} listed under both "
                        f"{devices[serial]!r} and {name.strip()!r} — a firewall sits "
                        f"under exactly one folder")
                devices[serial] = name.strip()
                if isinstance(dspec, dict) and "hostname" in dspec:
                    # REJECTED, not ignored. `hostname:` was never parsed by
                    # anything — a third decorative field in a file that
                    # otherwise drives behaviour — and it never held the
                    # hostname. Renaming it silently would leave the old key
                    # looking meaningful while doing nothing, exactly as before.
                    raise CatalogError(
                        f"folder hierarchy: {serial!r}.hostname is renamed to "
                        f"`display_name`. It never held the firewall's hostname (that is "
                        f"DHCP-assigned and undeclared) — it is the label SCM shows, which "
                        f"`fwgitops verify-catalog` now compares. Rename the key.")
                dname = dspec.get("display_name") if isinstance(dspec, dict) else None
                if dname is not None:
                    if not isinstance(dname, str) or not dname.strip():
                        raise CatalogError(
                            f"folder hierarchy: {serial!r}.display_name must be a "
                            f"non-empty string, got {dname!r}")
                    display_names[serial] = dname.strip()
                dflag = dspec.get("targetable") if isinstance(dspec, dict) else None
                if dflag is not None and not isinstance(dflag, bool):
                    raise CatalogError(
                        f"folder hierarchy: {serial!r}.targetable must be true or false, "
                        f"got {dflag!r}")
                if dflag is True:
                    targetable_devices.add(serial)

        # A serial must not also be a folder name — that is the v1.11.0 mistake
        # in its purest form, and it would make `folder:` and `device:` disagree
        # about what a name means.
        clash = sorted(set(devices) & set(out))
        if clash:
            raise CatalogError(
                f"folder hierarchy: {clash} appear as BOTH a folder and a firewall. "
                f"A firewall is the last level of the hierarchy but is addressed "
                f"`device=<serial>`, never `folder=<serial>`.")
        return cls(children=out, targetable=frozenset(targetable),
                   devices=devices, targetable_devices=frozenset(targetable_devices),
                   device_display_names=display_names)


@dataclass(frozen=True)
class InterfaceCatalog:
    """Logical interface ROLE -> the object name at each scope.

    The same interface has two names in SCM and which is correct depends on the
    scope being written (ADR-0005): `$eth-local` at folder scope, `ethernet1/4`
    at device scope, one object. An intent that hardcodes either is wrong
    somewhere — and the physical name is a property of the AWS topology, which
    has already changed once here.

    So intents name the role and this resolves it, at LOAD time, like
    RouterCatalog supplies VRF membership. Fail closed: an unknown role, or a
    role with no mapping for the target firewall, is an error rather than a
    guessed port.
    """

    #: role -> name at folder scope
    folder_names: Dict[str, str]
    #: role -> {serial -> physical name}
    device_names: Dict[str, Dict[str, str]]
    #: Roles that are NOT expected on every firewall.
    #:
    #: `local` and `internet` are universal here: every firewall this platform
    #: manages has both, so a firewall MISSING one is a catalog gap and a test
    #: asserts total coverage. That invariant is worth keeping — it is what
    #: stops an intent naming a device whose port was never mapped.
    #:
    #: It is the wrong invariant for a role like `dmz`, which is a property of
    #: one site's wiring: fw-prod-edge-4453 has an ENI at device index 2,
    #: fw-prod-edge-3662 is not known to. Listing a port for it would be a
    #: GUESS, and guessing ports is precisely what this catalog exists to
    #: prevent (ADR-0005 — the physical name has already changed once here).
    #:
    #: So a role that is not universal must SAY SO, and then the coverage test
    #: skips it. This is narrower than relaxing the test: an unmarked role is
    #: still required everywhere, and `resolve()` still fails closed for an
    #: unmapped firewall either way. The flag changes what is EXPECTED, never
    #: what is enforced at load.
    site_specific: FrozenSet[str] = frozenset()
    #: role -> {folder -> default_value}. The folder-scope VARIABLES this
    #: platform creates, as opposed to the SCM defaults it merely inherits.
    #: Opt-in: a role with no entry is assumed to come from somewhere else,
    #: because creating `$eth-local` in a child folder would SHADOW the object
    #: every firewall resolves `local` through rather than add anything.
    create_in: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def roles(self) -> List[str]:
        return sorted(self.folder_names)

    def universal_roles(self) -> List[str]:
        """Roles every targetable firewall is expected to have."""
        return [r for r in self.roles() if r not in self.site_specific]

    def known(self, role: str) -> bool:
        return role in self.folder_names

    def resolve(self, role: str, *, device: Optional[str]) -> str:
        """The object name to write for `role` at this scope.

        `device=None` means folder scope. Raises CatalogError, which the loader
        turns into an intent problem — never returns a fallback.
        """
        if role not in self.folder_names:
            raise CatalogError(
                f"unknown interface role {role!r}; known: {', '.join(self.roles()) or '(none)'}"
            )
        if device is None:
            return self.folder_names[role]
        by_device = self.device_names.get(role, {})
        name = by_device.get(device)
        if name is None:
            raise CatalogError(
                f"interface role {role!r} has no mapping for firewall {device!r} in "
                f"catalog/interfaces.yaml; mapped firewalls: "
                f"{', '.join(sorted(by_device)) or '(none)'}"
            )
        return name

    def folder_variables(self, folder: str) -> Dict[str, str]:
        """The `$`-variables this folder must materialise: name -> default_value.

        Empty for a folder that creates none, which is the normal case: a folder
        usually inherits every interface it needs.
        """
        out: Dict[str, str] = {}
        for role, by_folder in sorted(self.create_in.items()):
            if folder in by_folder:
                out[self.folder_names[role]] = by_folder[folder]
        return out

    def folder_variable_objects(self, folder: str) -> Dict[str, Dict[str, Any]]:
        """The `$`-variables this folder materialises, in provider shape.

        ONE definition, used by `fwgitops folder-interfaces` (which writes them)
        and by drift (which must recognise them). They are declared config — just
        declared in this catalog rather than in an intent — so without this the
        drift check reported `prod-edge/$eth-dmz` as "present in SCM, neither
        declared nor a known baseline object" on every run, forever.
        """
        return {
            name: {
                "name": name,
                "folder": folder,
                "device": None,
                "default_value": default_value,
                "comment": "folder interface variable — managed by fwgitops",
                # `{}` not None: the provider requires exactly one of
                # layer3/layer2/tap, and null satisfies none.
                "layer3": {},
            }
            for name, default_value in sorted(self.folder_variables(folder).items())
        }

    def create_in_conflicts(self, devices_of_folder) -> List[str]:
        """Where a folder's own firewalls contradict the port it declares.

        `default_value` is ONE value per folder object, so a role whose firewalls
        in that folder resolve to DIFFERENT physical ports cannot be expressed as
        a folder variable at all — the correct answer there is a device-scope
        override, which InterfaceRequest already does.

        Reported rather than resolved. Picking one port would send the other
        firewall's traffic out the wrong wire, silently, and be indistinguishable
        from working until someone looked at a packet.

        `devices_of_folder` is a callable folder -> iterable of serials (i.e.
        `FolderHierarchy.devices_of`), kept as a parameter so this module stays
        free of a hard dependency on the hierarchy.
        """
        problems: List[str] = []
        for role, by_folder in sorted(self.create_in.items()):
            for folder, declared in sorted(by_folder.items()):
                mapped = self.device_names.get(role, {})
                for serial in sorted(devices_of_folder(folder) or ()):
                    actual = mapped.get(serial)
                    if actual is not None and actual != declared:
                        problems.append(
                            f"interface role {role!r}: folder {folder!r} declares "
                            f"create_in={declared!r} but firewall {serial!r} in that "
                            f"folder maps to {actual!r}. One folder variable cannot be "
                            f"two ports — use a device-scope InterfaceRequest for the "
                            f"firewall that differs."
                        )
        return problems

    @classmethod
    def from_dict(cls, data: Any) -> "InterfaceCatalog":
        if not isinstance(data, dict):
            raise CatalogError("interface catalog must be a mapping")
        roles = data.get("interfaces", data)
        if not isinstance(roles, dict):
            raise CatalogError("interface catalog: `interfaces` must be a mapping")
        folder_names: Dict[str, str] = {}
        device_names: Dict[str, Dict[str, str]] = {}
        site_specific: set = set()
        create_in: Dict[str, Dict[str, str]] = {}
        for role, spec in roles.items():
            if not isinstance(role, str) or not role.strip():
                raise CatalogError(f"interface catalog: bad role name {role!r}")
            role = role.strip()
            if not isinstance(spec, dict):
                raise CatalogError(f"interface catalog: {role!r} must be a mapping")
            fname = spec.get("folder")
            if not isinstance(fname, str) or not fname.strip():
                raise CatalogError(
                    f"interface catalog: {role!r}.folder must be the folder-scope name "
                    f"(e.g. $eth-local)")
            folder_names[role] = fname.strip()
            devs = spec.get("devices", {}) or {}
            if not isinstance(devs, dict):
                raise CatalogError(
                    f"interface catalog: {role!r}.devices must be a mapping of "
                    f"serial -> physical name")
            out: Dict[str, str] = {}
            for serial, phys in devs.items():
                if isinstance(serial, int):
                    raise CatalogError(
                        f"interface catalog: device serial {serial!r} parsed as a number — "
                        f'quote it ("{serial}").')
                if not isinstance(serial, str) or not serial.strip():
                    raise CatalogError(f"interface catalog: bad device serial {serial!r}")
                if not isinstance(phys, str) or not phys.strip():
                    raise CatalogError(
                        f"interface catalog: {role}/{serial} must map to a physical "
                        f"interface name (e.g. ethernet1/4)")
                out[serial.strip()] = phys.strip()
            device_names[role] = out
            ci = spec.get("create_in") or {}
            if not isinstance(ci, dict):
                raise CatalogError(
                    f"interface catalog: {role!r}.create_in must be a mapping of "
                    f"folder -> physical interface name (the folder variable's "
                    f"default_value)")
            clean: Dict[str, str] = {}
            for fol, phys in ci.items():
                if not isinstance(fol, str) or not fol.strip():
                    raise CatalogError(f"interface catalog: {role!r}.create_in bad folder {fol!r}")
                if not isinstance(phys, str) or not phys.strip():
                    raise CatalogError(
                        f"interface catalog: {role}/create_in/{fol} must map to a physical "
                        f"interface name (e.g. ethernet1/2)")
                clean[fol.strip()] = phys.strip()
            if clean:
                create_in[role] = clean
            if spec.get("site_specific") is True:
                site_specific.add(role)
            elif spec.get("site_specific") not in (None, False):
                raise CatalogError(
                    f"interface catalog: {role!r}.site_specific must be true or false, "
                    f"got {spec.get('site_specific')!r}")
        return cls(folder_names=folder_names, device_names=device_names,
                   site_specific=frozenset(site_specific), create_in=create_in)


@dataclass(frozen=True)
class RouterCatalog:
    """Logical router / VRF topology, including interface membership.

    `scm_logical_router` holds a VRF's interface list AND its routes in one
    object, and Terraform manages whole objects. A RouteRequest declares a single
    route, so the compiler must aggregate — and without membership declared
    somewhere it would emit a router with routes and NO interfaces, breaking the
    object all traffic depends on.

    Declared rather than read live, so the compiler stays pure: the same intents
    always compile to the same output.
    """

    #: folder -> router -> vrf -> interface names
    routers: Dict[str, Dict[str, Dict[str, Tuple[str, ...]]]]

    def interfaces_for(self, folder: str, router: str, vrf: str) -> Optional[Tuple[str, ...]]:
        """Membership, or None when the folder/router/vrf is not declared."""
        return self.routers.get(folder, {}).get(router, {}).get(vrf)

    def known(self, folder: str) -> List[str]:
        out = []
        for router, vrfs in sorted(self.routers.get(folder, {}).items()):
            out.extend(f"{router}/{v}" for v in sorted(vrfs))
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "RouterCatalog":
        if not isinstance(data, dict):
            raise CatalogError("router catalog must be a mapping")
        folders = data.get("routers", data)
        if not isinstance(folders, dict):
            raise CatalogError("router catalog: `routers` must be a mapping")
        out: Dict[str, Dict[str, Dict[str, Tuple[str, ...]]]] = {}
        for folder, routers in folders.items():
            if not isinstance(routers, dict):
                raise CatalogError(f"router catalog: {folder!r} must be a mapping of routers")
            for router, spec in routers.items():
                vrfs = spec.get("vrfs") if isinstance(spec, dict) else None
                if not isinstance(vrfs, dict) or not vrfs:
                    raise CatalogError(
                        f"router catalog: {folder}/{router} needs a non-empty `vrfs` mapping")
                for vrf, vspec in vrfs.items():
                    ifaces = vspec.get("interfaces", []) if isinstance(vspec, dict) else None
                    if not isinstance(ifaces, list) or not all(
                        isinstance(i, str) and i.strip() for i in ifaces
                    ):
                        raise CatalogError(
                            f"router catalog: {folder}/{router}/{vrf}.interfaces must be a "
                            f"list of interface names")
                    out.setdefault(folder, {}).setdefault(router, {})[vrf] = tuple(
                        i.strip() for i in ifaces)
        return cls(routers=out)
