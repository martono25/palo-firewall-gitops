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

from dataclasses import dataclass
from typing import Any, Dict


class ResolveError(Exception):
    """Raised when an environment cannot be resolved. Actionable message."""


@dataclass(frozen=True)
class EnvResolution:
    folder: str
    from_zone: str
    to_zone: str


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
            out[env] = EnvResolution(
                folder=spec["folder"], from_zone=spec["from_zone"], to_zone=spec["to_zone"]
            )
        return cls(out)

    def baseline_zones_by_folder(self) -> Dict[str, set]:
        """Per folder, the default zones the env map declares (from_zone + to_zone).

        These are the baseline zones that already exist on the folder's device;
        additional zones must be declared by a ZoneRequest. Used by the cross-kind
        zone-consistency check so a rule can only reference a declared zone.
        """
        out: Dict[str, set] = {}
        for res in self._map.values():
            out.setdefault(res.folder, set()).update((res.from_zone, res.to_zone))
        return out

    def resolve(self, environment: str) -> EnvResolution:
        try:
            return self._map[environment]
        except KeyError:
            known = ", ".join(sorted(self._map)) or "(none configured)"
            raise ResolveError(
                f"unknown environment {environment!r}; known environments: {known}"
            ) from None
