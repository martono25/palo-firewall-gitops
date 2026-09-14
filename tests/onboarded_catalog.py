"""A copy of the REAL catalog with ONE FIREWALL ONBOARDED.

WHY THIS EXISTS. Device-scope behaviour — an InterfaceRequest resolving a role
to a port, a push record keyed by `device-<serial>`, a baseline naming a
firewall that has since been replaced — needs a targetable firewall to exist.
Until 2026-09-14 those tests borrowed the LIVE one from catalog/folders.yaml.

The live firewall is the least stable thing in this repository. Its serial went
891682 -> 893662 -> 894453 -> 901881 -> 902404, and each change was a repo-wide
rename through tests that were never about that firewall. One rename left
`_catalog_pair` pointing at a device that was already absent, so its "should
fail" case passed for the wrong reason. On 2026-09-14 the pilot's AWS account
expired, prod-edge was left with NO firewall at all, and ten tests failed at
once — on a change that broke no behaviour.

So a test about device scope asks for a firewall and gets one, whatever SCM
currently holds. Everything else is copied from the real catalog, so every
OTHER lookup still exercises the files that ship.

WHAT STAYS ON THE REAL CATALOG, deliberately: tests whose subject IS this
repository — "every shipped intent loads", "every rule references an object the
compiler emits". Those must fail when the real catalog and the real intents
disagree, and pointing them here would hide exactly that.

WHY THE EXAMPLE SERIAL IS A RETIRED ONE. `007955000902404` is the firewall that
died with the account. Documentation and `*.example.yaml` still show it. A
retired serial can never be registered again, so an example can never be
mistaken for — or collide with — a device that is live.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CATALOG = REPO_ROOT / "catalog"

#: The serial documentation and examples use. Retired 2026-09-14 — see above.
EXAMPLE_SERIAL = "007955000902404"

#: Role -> port, as the retired pilot was wired (ENI device index N ->
#: ethernet1/N). `dmz` agrees with `create_in.prod-edge`, which catalog.py
#: requires of every firewall mapped under that folder.
PORTS = {"local": "ethernet1/1", "internet": "ethernet1/2", "dmz": "ethernet1/3"}


def _read(name: str) -> Dict[str, Any]:
    return yaml.safe_load((REAL_CATALOG / name).read_text())


def onboarded_dicts(serial: str = EXAMPLE_SERIAL,
                    folder: str = "prod-edge") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(folders, interfaces) — the real catalogs with `serial` onboarded.

    Onboarding ADDS to what is there. If the real catalog already has a
    firewall, it stays, so a test cannot pass only because the live device
    disappeared from its view.
    """
    folders = copy.deepcopy(_read("folders.yaml"))
    entry = folders["folders"][folder]
    devices = entry.get("devices") or {}
    devices[serial] = {"display_name": f"fw-{folder}-{serial[-4:]}",
                       "model": "PA-VM", "targetable": True}
    entry["devices"] = devices

    interfaces = copy.deepcopy(_read("interfaces.yaml"))
    for role, port in PORTS.items():
        spec = interfaces["interfaces"].get(role)
        if spec is None:
            continue
        mapped = spec.get("devices") or {}
        mapped[serial] = port
        spec["devices"] = mapped
    return folders, interfaces


def onboarded_catalog_dir(dest: Path, serial: str = EXAMPLE_SERIAL) -> Path:
    """Copy the real catalog to `dest` with `serial` onboarded; return `dest`.

    For CLI entry points, which find their catalogs next to
    `service_catalog_path` rather than taking objects.
    """
    shutil.copytree(REAL_CATALOG, dest)
    folders, interfaces = onboarded_dicts(serial)
    (dest / "folders.yaml").write_text(yaml.safe_dump(folders, sort_keys=False))
    (dest / "interfaces.yaml").write_text(yaml.safe_dump(interfaces, sort_keys=False))
    return dest


def load_kwargs(serial: str = EXAMPLE_SERIAL) -> Dict[str, Any]:
    """`load_intent(**load_kwargs())` — every catalog, with `serial` onboarded."""
    from fwgitops.catalog import (
        FolderHierarchy, InterfaceCatalog, RouterCatalog, ServiceCatalog,
    )
    from fwgitops.resolve import EnvMap

    folders, interfaces = onboarded_dicts(serial)
    kw: Dict[str, Any] = dict(
        env_map=EnvMap.from_dict(_read("environments.yaml")),
        folder_hierarchy=FolderHierarchy.from_dict(folders),
        interface_catalog=InterfaceCatalog.from_dict(interfaces),
        router_catalog=RouterCatalog.from_dict(_read("routers.yaml")),
    )
    if (REAL_CATALOG / "services.yaml").is_file():
        kw["service_catalog"] = ServiceCatalog.from_dict(_read("services.yaml"))
    return kw
