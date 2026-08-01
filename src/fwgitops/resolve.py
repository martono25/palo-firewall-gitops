"""Phase-1 environment resolution (env → SCM folder + zone-pair).

A deliberately minimal, hand-maintained map used before the Phase-2 catalog
exists. Each environment resolves to one SCM folder and a default zone-pair:

    environment: prod  ─▶  {folder: prod-edge, from_zone: trust, to_zone: app}

Per-IP zone inference and the real folder/zone catalog arrive in Phase 2 (see
docs/DESIGN.md). This keeps the walking skeleton minimal while still producing
correctly-scoped rules on a known-topology pilot folder. FQDN destinations
(which have no compile-time IP) use the environment's to_zone.

Fails closed: an unknown environment raises with the list of known ones rather
than guessing a folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple


class ResolveError(Exception):
    """Raised when an environment cannot be resolved. Actionable message."""


@dataclass(frozen=True)
class EnvResolution:
    folder: str
    from_zone: str
    to_zone: str
    #: Other zones that already exist on this folder's device but are not the
    #: default pair. Optional; see `baseline_zones_by_folder` for why it matters.
    baseline_zones: Tuple[str, ...] = field(default=())


class EnvMap:
    """environment name → EnvResolution."""

    def __init__(self, mapping: Dict[str, EnvResolution]):
        self._map = dict(mapping)

    @classmethod
    def from_dict(cls, data: Any) -> "EnvMap":
        """Build from a plain dict (e.g. parsed YAML). Fails closed on bad shape."""
        if not isinstance(data, dict) or not data:
            raise ResolveError("environment map must be a non-empty mapping")
        out: Dict[str, EnvResolution] = {}
        for env, spec in data.items():
            if not isinstance(spec, dict):
                raise ResolveError(f"environment {env!r}: entry must be a mapping")
            missing = [k for k in ("folder", "from_zone", "to_zone") if not spec.get(k)]
            if missing:
                raise ResolveError(f"environment {env!r}: missing {missing}")
            extra = spec.get("baseline_zones", [])
            if not isinstance(extra, list) or not all(
                isinstance(z, str) and z.strip() for z in extra
            ):
                raise ResolveError(
                    f"environment {env!r}: baseline_zones must be a list of zone names (strings)"
                )
            out[env] = EnvResolution(
                folder=spec["folder"],
                from_zone=spec["from_zone"],
                to_zone=spec["to_zone"],
                baseline_zones=tuple(extra),
            )
        return cls(out)

    def baseline_zones_by_folder(self) -> Dict[str, set]:
        """Per folder, the zones that already exist on the device.

        This is the default pair (from_zone + to_zone) PLUS any `baseline_zones`
        the env map declares. The extra list exists because a device carries more
        zones than the default pair: the pilot tenant has seven per folder
        (zone-internal, zone-to-hub, zone-to-branch, zone-to-pa-hub, local,
        internet, proxy) while the map named only two.

        Deriving this set from from_zone/to_zone alone made the cross-kind check
        REJECT a rule that referenced a real zone such as `proxy` — a fail-closed
        check producing a false negative. Zones listed here are asserted to exist
        on the device; zones that do NOT exist still need a ZoneRequest.
        """
        out: Dict[str, set] = {}
        for res in self._map.values():
            out.setdefault(res.folder, set()).update(
                (res.from_zone, res.to_zone, *res.baseline_zones)
            )
        return out

    def resolve(self, environment: str) -> EnvResolution:
        try:
            return self._map[environment]
        except KeyError:
            known = ", ".join(sorted(self._map)) or "(none configured)"
            raise ResolveError(
                f"unknown environment {environment!r}; known environments: {known}"
            ) from None
