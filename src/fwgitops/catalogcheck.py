"""Compare `catalog/folders.yaml` against SCM's real hierarchy.

WHY. The catalog is a HAND-MAINTAINED MIRROR of a hierarchy that changes
underneath it, and nothing verified the two. It is declared rather than read live
on purpose — the classifier and compiler are pure, so the same intents always
compile to the same output — but "pure" only buys determinism, not truth.

It has already gone wrong twice, in opposite directions:

  * 2026-08-02 (v1.11.0): device serials were listed as targetable CHILD FOLDERS.
    They are `type: on-prem` entries, not containers, so an intent naming one as
    `folder:` compiled clean and failed at apply.
  * 2026-08-05: `007955000893662` disappeared from SCM entirely while the catalog
    went on listing it as targetable with port mappings. Same failure shape:
    compiles clean, dies at apply, against a firewall that no longer exists.

Both are the failure mode this platform designs against, arriving through the
catalog instead of the compiler — where none of the compile-time checks look.

THE DIRECTION OF THE CHECK MATTERS. Objects in SCM that the catalog does not
mention are NOT reported: Prisma Access built-ins (`Mobile Users`, `Remote
Networks`, …) are deliberately absent because this platform does not manage them,
and a check that flags them every run is a check people learn to ignore. What is
reported is the catalog CLAIMING something SCM contradicts.

This module is pure. The SCM read is the caller's job, so the comparison is
testable without a tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: SCM's own discriminator on `GET /config/setup/v1/folders`. A container is a
#: real folder; an on-prem entry is a DEVICE, carrying serial_number and model.
TYPE_CONTAINER = "container"
TYPE_DEVICE = "on-prem"


@dataclass(frozen=True)
class LiveEntry:
    """One entry from `GET /config/setup/v1/folders`, normalised."""

    name: str
    type: str
    parent: Optional[str] = None

    @property
    def is_device(self) -> bool:
        return self.type == TYPE_DEVICE


@dataclass(frozen=True)
class Finding:
    """One divergence. `blocking` decides the exit code, not the wording."""

    subject: str
    message: str
    blocking: bool = True

    def __str__(self) -> str:
        return f"{self.subject}: {self.message}"


def parse_live(rows: Iterable[dict]) -> Dict[str, LiveEntry]:
    """Normalise the API payload. Keyed by name; a device's name IS its serial."""
    out: Dict[str, LiveEntry] = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        out[name] = LiveEntry(
            name=name,
            type=row.get("type") or "",
            parent=row.get("parent") or None,
        )
    return out


def compare(hierarchy: Any, live: Dict[str, LiveEntry]) -> List[Finding]:
    """Every way `catalog/folders.yaml` contradicts SCM.

    `hierarchy` is a `catalog.FolderHierarchy`; passed in rather than imported so
    this module stays a pure comparison.
    """
    findings: List[Finding] = []

    declared_folders = sorted(set(hierarchy.children) | {
        c for kids in hierarchy.children.values() for c in kids})

    for folder in declared_folders:
        entry = live.get(folder)
        if entry is None:
            findings.append(Finding(
                f"folder {folder!r}",
                "declared in catalog/folders.yaml but ABSENT from SCM. An intent "
                "targeting it compiles clean and fails at apply.",
                blocking=hierarchy.is_targetable(folder),
            ))
            continue
        if entry.is_device:
            # The v1.11.0 mistake, in the shape that made it dangerous: a device
            # listed where a folder is expected. `folder=<serial>` is rejected
            # by SCM with "Folder doesn't exist", so this compiles and dies.
            findings.append(Finding(
                f"folder {folder!r}",
                f"is declared as a FOLDER but SCM reports type={entry.type!r} — it is "
                f"a DEVICE. A firewall is the last level of the hierarchy but is "
                f"addressed `device=<serial>`, never `folder=`. Move it under the "
                f"parent folder's `devices:` block.",
            ))

    # Parent relationships. A folder that moved out of band is exactly the change
    # re-parenting support (v2.0) has to survive, so it is caught here first.
    for parent, kids in sorted(hierarchy.children.items()):
        for child in sorted(kids):
            entry = live.get(child)
            if entry is None or entry.is_device:
                continue                      # already reported above
            if entry.parent != parent:
                findings.append(Finding(
                    f"folder {child!r}",
                    f"catalog says its parent is {parent!r}; SCM says "
                    f"{entry.parent!r}. Config inherits DOWN the folder tree, so a "
                    f"wrong parent means the blast radius recorded here is wrong.",
                ))

    # A TARGETABLE FOLDER THAT NO FIREWALL INHERITS. Objects compiled into it
    # reach nothing: the compile succeeds, the apply succeeds, the push succeeds
    # trivially, and no packet is ever filtered. Every signal is green and the
    # rule does not exist anywhere it matters.
    #
    # NOT blocking. It is the normal state of a folder during Day-1: ADR-0002 has
    # the folder created BEFORE the firewall registers to it (the firewall names
    # it as `dgname`), so a new folder is legitimately empty for a while. Failing
    # here would break the documented bring-up order. Reported instead, so an
    # empty folder is a thing you know about rather than discover.
    with_devices = {f for f in hierarchy.devices.values()}
    for folder in sorted(hierarchy.targetable_folders()):
        if folder in with_devices:
            continue
        kids = hierarchy.children_of(folder) if hasattr(hierarchy, "children_of") else ()
        if kids:
            continue          # a parent inherits down to its children's firewalls
        findings.append(Finding(
            f"folder {folder!r}",
            "is targetable but NO FIREWALL inherits from it. Objects compiled here "
            "reach no device: compile, apply and push all succeed and nothing is "
            "enforced. Normal while a folder waits for its firewall to register; "
            "worth checking if it is not.",
            blocking=False,
        ))

    for serial, folder in sorted(hierarchy.devices.items()):
        entry = live.get(serial)
        targetable = hierarchy.is_device_targetable(serial)
        if entry is None:
            findings.append(Finding(
                f"firewall {serial!r}",
                "declared in catalog/folders.yaml but ABSENT from SCM — it is not "
                "registered, or has been removed."
                + ("" if targetable else
                   " Already marked `targetable: false`, so no intent can name it; "
                   "reported so the entry is not forgotten, not as a failure."),
                blocking=targetable,
            ))
            continue
        if not entry.is_device:
            findings.append(Finding(
                f"firewall {serial!r}",
                f"is declared as a DEVICE but SCM reports type={entry.type!r}. "
                f"A container addressed as `device=` is rejected on write.",
            ))
            continue
        if entry.parent != folder:
            findings.append(Finding(
                f"firewall {serial!r}",
                f"catalog places it under {folder!r}; SCM says {entry.parent!r}. "
                f"A firewall inherits from its parent folder, so its zones, routes "
                f"and rules come from a different folder than this repo believes.",
            ))

    return findings


def compare_interfaces(interface_catalog: Any, hierarchy: Any,
                       live: Dict[str, LiveEntry]) -> List[Finding]:
    """Serials mapped in `catalog/interfaces.yaml` that SCM does not have.

    Separate from `compare` because a stale interface mapping is a smaller
    problem than a stale hierarchy: it only bites an intent that names that
    firewall for that role. Still worth reporting — it is the other half of the
    3662 staleness, and deleting the device entry without these leaves a mapping
    pointing at nothing.
    """
    findings: List[Finding] = []
    for role in sorted(interface_catalog.device_names):
        for serial in sorted(interface_catalog.device_names[role]):
            if serial in live:
                continue
            findings.append(Finding(
                f"interface role {role!r}",
                f"maps firewall {serial!r}, which is ABSENT from SCM.",
                blocking=hierarchy.is_device_targetable(serial),
            ))
    return findings
