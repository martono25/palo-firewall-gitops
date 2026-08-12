"""Folder-scope `$`-interface VARIABLES (ADR-0005, greenfield support).

A folder-scope zone can only bind an interface object that exists AT THAT SCOPE
— binding a literal port is refused by SCM as an invalid reference. `$eth-local`
and `$eth-internet` exist only because they are SCM defaults inherited from
`ngfw-shared`; a NEW role, or a NEW folder wanting a role of its own, has no such
default. That is what stopped "Day-1 provisioning as GitOps" being true of a
folder that starts empty.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.catalog import CatalogError, FolderHierarchy, InterfaceCatalog  # noqa: E402
from fwgitops.cli import run_folder_interfaces  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

FOLDERS = """
folders:
  ngfw-shared:
    children: [prod-edge]
    targetable: false
  prod-edge:
    children: []
    targetable: true
    devices:
      "007955000902404":
        display_name: fw-a
        model: PA-VM
        targetable: true
"""

CATALOG = """
interfaces:
  local:
    folder: $eth-local
    devices:
      "007955000902404": ethernet1/4
  dmz:
    folder: $eth-dmz
    site_specific: true
    create_in:
      prod-edge: ethernet1/2
    devices:
      "007955000902404": ethernet1/2
"""


def _cat(text=CATALOG):
    return InterfaceCatalog.from_dict(yaml.safe_load(text))


def _hier(text=FOLDERS):
    return FolderHierarchy.from_dict(yaml.safe_load(text))


def test_only_roles_that_opt_in_are_materialised():
    """Opt-in, and the direction matters: creating `$eth-local` in a CHILD folder
    would SHADOW the object every firewall resolves `local` through, rather than
    add anything. Silence must mean "inherited", never "create it here"."""
    cat = _cat()
    assert cat.folder_variables("prod-edge") == {"$eth-dmz": "ethernet1/2"}
    assert "$eth-local" not in cat.folder_variables("prod-edge")


def test_a_folder_that_creates_nothing_gets_no_file(tmp_path):
    cat = _cat()
    assert cat.folder_variables("ngfw-shared") == {}


def test_a_folder_whose_firewalls_disagree_is_REJECTED():
    """`default_value` is ONE value per folder object. If two firewalls in the
    folder resolve the role to different ports, no single value is correct.

    Picking one would send the other firewall's traffic out the wrong wire,
    silently, and look identical to working until someone read a packet. The
    correct answer is a device-scope InterfaceRequest, so this reports instead of
    choosing.
    """
    folders = FOLDERS + """      "007955000893662":
        display_name: fw-b
        model: PA-VM
        targetable: true
"""
    catalog = CATALOG + '      "007955000893662": ethernet1/5\n'
    problems = _cat(catalog).create_in_conflicts(_hier(folders).devices_of)
    assert len(problems) == 1
    assert "cannot be two ports" in problems[0]
    assert "007955000893662" in problems[0] and "ethernet1/5" in problems[0]


def test_agreement_is_not_a_conflict():
    """The guard must fire on DISAGREEMENT, not on having more than one firewall
    — otherwise it would block every multi-firewall folder."""
    folders = FOLDERS + """      "007955000893662":
        display_name: fw-b
        model: PA-VM
        targetable: true
"""
    catalog = CATALOG + '      "007955000893662": ethernet1/2\n'
    assert _cat(catalog).create_in_conflicts(_hier(folders).devices_of) == []


def test_the_emitted_variable_has_no_addressing(tmp_path):
    """An address must match ONE firewall's ENI. On a shared folder object it
    would hand the same IP to every firewall inheriting the folder."""
    (tmp_path / "interfaces.yaml").write_text(CATALOG)
    (tmp_path / "folders.yaml").write_text(FOLDERS)
    out = tmp_path / "terraform"
    (out / "prod-edge").mkdir(parents=True)
    rc = run_folder_interfaces(out, interface_catalog_path=tmp_path / "interfaces.yaml",
                               folders_path=tmp_path / "folders.yaml")
    assert rc == 0
    data = json.loads((out / "prod-edge" / "interface_vars.auto.tfvars.json").read_text())
    var = data["folder_interfaces"]["$eth-dmz"]
    assert var["default_value"] == "ethernet1/2"
    assert var["folder"] == "prod-edge"
    assert var["device"] is None
    # `{}` not None: the provider requires exactly one of layer3/layer2/tap, and
    # null satisfies none — the plan fails rather than the interface being wrong,
    # but it fails for a reason nobody would guess from the intent.
    assert var["layer3"] == {}


def test_a_conflict_writes_nothing_at_all(tmp_path):
    """Fail closed: a rejected run must not leave a partially-materialised
    folder behind, the same all-or-nothing contract `compile` keeps."""
    (tmp_path / "interfaces.yaml").write_text(
        CATALOG + '      "007955000893662": ethernet1/5\n')
    (tmp_path / "folders.yaml").write_text(FOLDERS + """      "007955000893662":
        display_name: fw-b
        model: PA-VM
        targetable: true
""")
    out = tmp_path / "terraform"
    (out / "prod-edge").mkdir(parents=True)
    rc = run_folder_interfaces(out, interface_catalog_path=tmp_path / "interfaces.yaml",
                               folders_path=tmp_path / "folders.yaml")
    assert rc == 2
    assert not (out / "prod-edge" / "interface_vars.auto.tfvars.json").exists()


def test_the_shipped_catalog_has_no_conflicts():
    cat = InterfaceCatalog.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "interfaces.yaml").read_text()))
    hier = FolderHierarchy.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))
    assert cat.create_in_conflicts(hier.devices_of) == []


def test_no_inherited_scm_default_is_ever_created():
    """`$eth-local` and `$eth-internet` are defined in ngfw-shared and inherited.
    Listing either under `create_in` would shadow the shared object for one
    folder — a silent, per-folder re-pointing of the interface every firewall in
    it resolves that role through."""
    cat = InterfaceCatalog.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "interfaces.yaml").read_text()))
    for inherited in ("local", "internet"):
        assert inherited not in cat.create_in, (
            f"{inherited} is an SCM default inherited from ngfw-shared; creating it "
            f"in a child folder shadows the shared object")


def test_a_non_dollar_prefixed_variable_is_REJECTED(tmp_path):
    """The module merges folder_interfaces with the compiled interfaces into one
    for_each. That is only safe while the key spaces are disjoint — and `merge`
    gives no diagnostic when they are not, it just drops one side."""
    (tmp_path / "interfaces.yaml").write_text("""
interfaces:
  dmz:
    folder: ethernet1/2
    create_in:
      prod-edge: ethernet1/2
    devices:
      "007955000902404": ethernet1/2
""")
    (tmp_path / "folders.yaml").write_text(FOLDERS)
    out = tmp_path / "terraform"
    (out / "prod-edge").mkdir(parents=True)
    rc = run_folder_interfaces(out, interface_catalog_path=tmp_path / "interfaces.yaml",
                               folders_path=tmp_path / "folders.yaml")
    assert rc == 2
    assert not (out / "prod-edge" / "interface_vars.auto.tfvars.json").exists()


def test_folder_variables_are_DECLARED_config_for_drift():
    """`$eth-dmz` is written by `fwgitops folder-interfaces` and managed by
    Terraform — it is declared, just in the catalog rather than in an intent.

    Drift's declared set is intent-derived, so without this it reported
    `prod-edge/$eth-dmz` as "present in SCM, neither declared nor a known
    baseline object" on every run, forever. One catalog method builds the shape
    for both the writer and the checker, so they cannot disagree.
    """
    objs = _cat().folder_variable_objects("prod-edge")
    assert set(objs) == {"$eth-dmz"}
    o = objs["$eth-dmz"]
    assert o["folder"] == "prod-edge" and o["device"] is None
    assert o["default_value"] == "ethernet1/2"
    assert o["layer3"] == {}, "null satisfies none of layer3/layer2/tap"


def test_a_folder_that_materialises_nothing_declares_nothing():
    assert _cat().folder_variable_objects("ngfw-shared") == {}
