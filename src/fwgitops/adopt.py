"""Point the repository at a firewall, using SCM as the source of truth.

WHY THIS EXISTS. Adopting a firewall — first build or replacement — was
seventeen hand edits across two catalogs, three intents, a directory name and a
Terraform root. The operator who walked it put it plainly: *"too many manual task
to edit i.e. folders, device scope and rules which is prone to error and typo."*

Every one of those edits transcribed something SCM already knows:

    catalog/folders.yaml     serial, display_name    GET /config/setup/v1/folders
                                                     GET /config/setup/v1/devices
    catalog/interfaces.yaml  role -> physical port   the folder variable's
                                                     `default_value`
    intent spec.device       the serial              the same serial

So the values are READ, not typed. That removes the typo, and it closes a real
gap as a side effect: nothing compared `catalog/interfaces.yaml` to SCM, so a
wrong port there configured the wrong interface with no error at any stage. A
value read from SCM cannot disagree with SCM.

WHAT IT WILL NOT DO. It refuses when the device is not in the folder you named,
when a role's variable cannot be read, or when the port map would be a guess.
A catalog that is wrong in a way nothing checks is worse than one that is
missing, so this fails closed rather than writing a plausible default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


class AdoptError(Exception):
    """Adoption cannot proceed. The message names what SCM said."""


class DeviceReader(Protocol):
    """The reads adoption needs. Narrow on purpose — this writes nothing to SCM."""

    def device_folder(self, serial: str) -> Optional[str]: ...

    def device_display_name(self, serial: str) -> Optional[str]: ...

    def folder_interface_variables(self, folder: str) -> Dict[str, str]:
        """`$eth-name -> default_value` for one folder, inherited included."""


@dataclass(frozen=True)
class Adoption:
    """What the repository should say about this firewall, read from SCM."""

    serial: str
    folder: str
    display_name: Optional[str]
    #: role -> physical port, resolved through the folder variable each role names
    ports: Dict[str, str] = field(default_factory=dict)
    #: roles the catalog declares that SCM has no variable for
    unresolved: List[str] = field(default_factory=list)


def plan_adoption(reader: DeviceReader, serial: str, *, folder: str,
                  roles: Dict[str, str]) -> Adoption:
    """Read SCM and return what the catalog should hold. Writes nothing.

    `roles` is `role -> folder variable name` from `catalog/interfaces.yaml`
    (`local -> $eth-local`), which is the one thing the repository legitimately
    decides: which logical roles this platform uses. The PORT each resolves to is
    SCM's to say.

    Raises rather than guessing:
      * the device is not in SCM, or sits in a different folder — adopting it
        into the folder you named would make the catalog assert a placement that
        is not real, and every later check compares against the catalog.
      * a role's variable has no `default_value` — a role with no port is not a
        role with a default port.
    """
    seen = reader.device_folder(serial)
    if seen is None:
        raise AdoptError(
            f"SCM does not place device {serial!r} in any folder. It may not have "
            f"registered yet — check `show cloud-management-status` on the "
            f"firewall — or the serial-number onboarding rule did not match it.")
    if seen != folder:
        raise AdoptError(
            f"device {serial!r} is in folder {seen!r}, not {folder!r}. Adopting it "
            f"into {folder!r} would make the catalog assert a placement that is "
            f"not real, and every later check trusts the catalog.")

    variables = reader.folder_interface_variables(folder)
    ports: Dict[str, str] = {}
    unresolved: List[str] = []
    for role, variable in sorted(roles.items()):
        port = variables.get(variable)
        if port:
            ports[role] = port
        else:
            unresolved.append(role)

    if not ports:
        raise AdoptError(
            f"none of the interface roles {sorted(roles)} resolved to a port in "
            f"folder {folder!r}. SCM reported variables: {sorted(variables)}. "
            f"Without a port map an intent naming a role cannot compile, so there "
            f"is nothing useful to write.")

    return Adoption(serial=serial, folder=folder,
                    display_name=reader.device_display_name(serial),
                    ports=ports, unresolved=unresolved)


# ── writing the plan into the repository ──────────────────────────────────
@dataclass
class Written:
    """What changed on disk, for the report and for `--check` to print."""

    changed: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)


def apply_adoption(adoption: "Adoption", *, folders_text: str, interfaces_text: str,
                   intent_files: Dict[str, str],
                   replacing: Optional[str] = None) -> Dict[str, str]:
    """Return `{path: new content}` for every file the adoption changes.

    PURE. It takes text and returns text, so `--check` and the real run are the
    same code path minus the write — rather than two implementations that can
    disagree about what would happen.

    Editing YAML as TEXT is deliberate. `catalog/interfaces.yaml` is 100 lines of
    comments explaining why each mapping is what it is — which port the ENI sits
    behind, why a role is `site_specific`, what `create_in` shadows. Round-
    tripping it through a YAML parser would discard every one of them, and those
    comments are the reason the file is followable at all.
    """
    out: Dict[str, str] = {}

    if replacing and replacing != adoption.serial:
        # The serial appears in both catalogs and in every device-scoped intent.
        # A plain replace is right precisely BECAUSE the string is a serial: it
        # is unambiguous, and a partial rename is the failure mode this command
        # exists to remove.
        if replacing in folders_text:
            out["catalog/folders.yaml"] = folders_text.replace(replacing, adoption.serial)
        if replacing in interfaces_text:
            out["catalog/interfaces.yaml"] = interfaces_text.replace(
                replacing, adoption.serial)
        for path, text in intent_files.items():
            if replacing in text:
                out[path] = text.replace(replacing, adoption.serial)

    # The port map is authoritative from SCM, so rewrite each role's entry for
    # this serial even when the serial itself did not change — that is what makes
    # a re-run correct a catalog that has drifted from the tenant.
    interfaces = out.get("catalog/interfaces.yaml", interfaces_text)
    for role, port in adoption.ports.items():
        interfaces = _set_device_port(interfaces, role, adoption.serial, port)
    if interfaces != interfaces_text:
        out["catalog/interfaces.yaml"] = interfaces

    if adoption.display_name:
        folders = out.get("catalog/folders.yaml", folders_text)
        folders = _set_display_name(folders, adoption.serial, adoption.display_name)
        if folders != folders_text:
            out["catalog/folders.yaml"] = folders

    return out


def _set_device_port(text: str, role: str, serial: str, port: str) -> str:
    """Set `<serial>: <port>` inside `interfaces.<role>.devices`, leaving comments.

    Scoped to the role's own block, because every role has a `devices:` map and a
    global replace would rewrite all of them to one port.
    """
    import re

    role_at = re.search(rf"^  {re.escape(role)}:\s*$", text, re.M)
    if not role_at:
        return text
    nxt = re.search(r"^  \S", text[role_at.end():], re.M)
    end = role_at.end() + (nxt.start() if nxt else len(text) - role_at.end())
    block = text[role_at.end():end]

    # `[^\n]*` rather than `\s*`: in multiline mode `\s*$` is greedy enough to
    # swallow the newline AND any blank line after it, and the replacement then
    # ate them. A command that silently reformats the file it edits is one people
    # stop trusting — the first live run deleted two blank lines and nothing
    # else, which is exactly the kind of diff that makes a real change hard to
    # see.
    entry = re.search(rf'^(\s+)"{re.escape(serial)}":[^\n]*$', block, re.M)
    if entry:
        new_block = (block[:entry.start()]
                     + f'{entry.group(1)}"{serial}": {port}'
                     + block[entry.end():])
        return text[:role_at.end()] + new_block + text[end:]

    devices_at = re.search(r"^(\s+)devices:\s*$", block, re.M)
    if not devices_at:
        return text
    insert = devices_at.end() + 1
    indent = devices_at.group(1) + "  "
    return (text[:role_at.end()] + block[:insert]
            + f'{indent}"{serial}": {port}\n' + block[insert:] + text[end:])


def _set_display_name(text: str, serial: str, name: str) -> str:
    """Set `display_name:` under a serial in `catalog/folders.yaml`.

    A stale one is why `verify-catalog` reports a note that reads like a
    re-onboard — the dangerous cause — for what is usually just an un-updated
    catalog. Reading it from SCM removes the ambiguity at the source.
    """
    import re

    at = re.search(rf'^(\s+)"{re.escape(serial)}":\s*$', text, re.M)
    if not at:
        return text
    nxt = re.search(r"^\s{0,%d}\S" % len(at.group(1)), text[at.end():], re.M)
    end = at.end() + (nxt.start() if nxt else len(text) - at.end())
    block = text[at.end():end]
    dn = re.search(r"^(\s+)display_name:.*$", block, re.M)
    if not dn:
        return text
    return text[:at.end()] + block[:dn.start()] + f"{dn.group(1)}display_name: {name}" \
        + block[dn.end():] + text[end:]
