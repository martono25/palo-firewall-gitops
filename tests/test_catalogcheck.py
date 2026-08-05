"""catalog/folders.yaml vs SCM's real hierarchy.

The catalog is declared rather than read live so the compiler and classifier stay
PURE — the same intents always compile to the same output. Purity buys
determinism, not truth, and nothing was checking the truth. It has gone wrong
twice in opposite directions (v1.11.0: devices listed as child folders;
2026-08-05: a firewall that left SCM entirely), both producing the same failure:
an intent that compiles clean and dies at apply.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.catalog import FolderHierarchy, InterfaceCatalog  # noqa: E402
from fwgitops.catalogcheck import (  # noqa: E402
    compare, compare_interfaces, parse_live,
)
from fwgitops.cli import run_verify_catalog  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

CATALOG = """
folders:
  ngfw-shared:
    children: [prod-edge]
    targetable: false
  prod-edge:
    children: []
    targetable: true
    devices:
      "007955000894453":
        display_name: fw-a
        model: PA-VM
        targetable: true
"""

LIVE_OK = [
    {"name": "All", "type": "container", "parent": ""},
    {"name": "ngfw-shared", "type": "container", "parent": "All"},
    {"name": "prod-edge", "type": "container", "parent": "ngfw-shared"},
    {"name": "007955000894453", "type": "on-prem", "parent": "prod-edge",
     "serial_number": "007955000894453", "model": "PA-VM"},
]


def _h(text=CATALOG):
    return FolderHierarchy.from_dict(yaml.safe_load(text))


def _live(rows=None):
    return parse_live(rows if rows is not None else LIVE_OK)


def test_a_matching_catalog_reports_nothing():
    assert compare(_h(), _live()) == []


def test_scm_objects_the_catalog_ignores_are_NOT_reported():
    """Prisma Access built-ins are deliberately absent — this platform does not
    manage them. A check that flags them every run is one people learn to
    ignore, which costs more than it catches."""
    rows = LIVE_OK + [
        {"name": "Prisma Access", "type": "container", "parent": "All"},
        {"name": "Mobile Users", "type": "container", "parent": "Mobile Users Container"},
    ]
    assert compare(_h(), _live(rows)) == []


def test_a_firewall_that_left_scm_is_BLOCKING_while_targetable():
    """2026-08-05, exactly. 007955000893662 vanished from SCM and the catalog went
    on listing it as targetable with port mappings."""
    cat = CATALOG + """      "007955000893662":
        display_name: fw-gone
        model: PA-VM
        targetable: true
"""
    findings = compare(_h(cat), _live())
    assert len(findings) == 1
    assert findings[0].blocking
    assert "ABSENT from SCM" in findings[0].message


def test_the_same_firewall_marked_non_targetable_is_reported_but_NOT_blocking():
    """`targetable: false` IS the operator's acknowledgement — no intent can name
    it. Failing anyway would train people to ignore the check, which is how a
    real divergence gets waved through."""
    cat = CATALOG + """      "007955000893662":
        display_name: fw-gone
        model: PA-VM
        targetable: false
"""
    findings = compare(_h(cat), _live())
    assert len(findings) == 1
    assert not findings[0].blocking


def test_a_device_declared_as_a_FOLDER_is_caught():
    """The v1.11.0 mistake. A firewall is the last level of the hierarchy, but is
    addressed `device=<serial>` — `folder=<serial>` returns 400 "Folder doesn't
    exist", so such an intent compiles clean and dies at apply."""
    cat = """
folders:
  ngfw-shared:
    children: ["007955000894453"]
    targetable: false
  "007955000894453":
    children: []
    targetable: true
"""
    findings = compare(_h(cat), _live())
    assert any("it is a DEVICE" in f.message.replace("— ", "") or "DEVICE" in f.message
               for f in findings)
    assert all(f.blocking for f in findings if "DEVICE" in f.message)


def test_a_folder_that_moved_under_a_different_parent_is_caught():
    """Config inherits DOWN the tree, so a wrong parent means the blast radius
    this repo records is wrong. It is also exactly the change v2.0 re-parenting
    has to survive, which is why the check comes first."""
    rows = [dict(r) for r in LIVE_OK]
    for r in rows:
        if r["name"] == "prod-edge":
            r["parent"] = "All"
    findings = compare(_h(), _live(rows))
    assert len(findings) == 1
    assert "parent" in findings[0].message and findings[0].blocking


def test_a_firewall_under_a_different_folder_is_caught():
    """A firewall inherits its zones, routes and rules from its parent. If SCM
    parents it elsewhere, this repo is managing the wrong folder for it — the
    v2.0 re-parenting case, arriving by surprise."""
    rows = [dict(r) for r in LIVE_OK]
    for r in rows:
        if r["name"] == "007955000894453":
            r["parent"] = "GitOps"
    findings = compare(_h(), _live(rows))
    assert len(findings) == 1
    assert "GitOps" in findings[0].message and findings[0].blocking


def test_a_declared_folder_missing_from_scm_is_caught():
    rows = [r for r in LIVE_OK if r["name"] != "prod-edge"]
    findings = compare(_h(), _live(rows))
    assert any("ABSENT from SCM" in f.message and f.blocking for f in findings)


def test_interface_mappings_for_a_vanished_firewall_are_reported():
    """The other half of the 3662 staleness: deleting the device entry without
    these leaves a role mapping pointing at nothing."""
    ifcat = InterfaceCatalog.from_dict(yaml.safe_load("""
interfaces:
  local:
    folder: $eth-local
    devices:
      "007955000894453": ethernet1/4
      "007955000893662": ethernet1/4
"""))
    findings = compare_interfaces(ifcat, _h(), _live())
    assert len(findings) == 1
    assert "007955000893662" in findings[0].message


def test_an_empty_hierarchy_fails_closed(tmp_path, capsys):
    """A read that returns nothing must not be read as "nothing is wrong". It
    would make every declared folder look absent — or, if the comparison were
    ever inverted, make everything pass."""
    class _Empty:
        def request(self, *a, **k):
            return {"data": []}

    (tmp_path / "folders.yaml").write_text(CATALOG)
    (tmp_path / "interfaces.yaml").write_text(
        'interfaces:\n  local:\n    folder: $eth-local\n    devices: {}\n')
    rc = run_verify_catalog(folders_path=tmp_path / "folders.yaml",
                            interface_catalog_path=tmp_path / "interfaces.yaml",
                            session=_Empty())
    assert rc == 1
    assert "refusing to compare against an empty hierarchy" in capsys.readouterr().err


def test_an_scm_read_failure_is_an_error_not_a_pass(tmp_path, capsys):
    """Fail closed on transport trouble too — a check that passes when it could
    not reach the thing it checks is worse than no check."""
    class _Broken:
        def request(self, *a, **k):
            raise RuntimeError("connection reset")

    (tmp_path / "folders.yaml").write_text(CATALOG)
    (tmp_path / "interfaces.yaml").write_text(
        'interfaces:\n  local:\n    folder: $eth-local\n    devices: {}\n')
    rc = run_verify_catalog(folders_path=tmp_path / "folders.yaml",
                            interface_catalog_path=tmp_path / "interfaces.yaml",
                            session=_Broken())
    assert rc == 1
    assert "connection reset" in capsys.readouterr().err


def test_the_shipped_catalog_parses_and_compares(tmp_path):
    """Not a tenant check — that needs credentials and runs in CI. This asserts
    the shipped catalog is well-formed enough for the comparison to run at all."""
    h = FolderHierarchy.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))
    assert compare(h, _live()) is not None


def test_a_targetable_folder_with_no_firewall_is_NOTED_not_blocking():
    """Objects compiled there reach no device: compile, apply and push all
    succeed and nothing is enforced.

    Non-blocking on purpose. ADR-0002 creates the folder BEFORE the firewall
    registers to it, so an empty folder is the normal state during bring-up —
    failing would break the documented order and train people to ignore this.
    """
    cat = CATALOG + """  GitOps:
    children: []
    targetable: true
"""
    rows = LIVE_OK + [{"name": "GitOps", "type": "container", "parent": "ngfw-shared"}]
    findings = compare(_h(cat), _live(rows))
    empty = [f for f in findings if "NO FIREWALL" in f.message]
    assert len(empty) == 1
    assert not empty[0].blocking
    assert "GitOps" in empty[0].subject


def test_a_parent_folder_is_not_reported_as_empty():
    """`ngfw-shared` has no firewall of its own but inherits down to `prod-edge`,
    so config placed there does reach devices. Reporting it would be noise on
    every run, which is how a real finding later gets skipped."""
    findings = compare(_h(), _live())
    assert not [f for f in findings if "NO FIREWALL" in f.message]


def test_a_reset_display_name_is_NOTED():
    """Cosmetic on its own — and a reliable symptom of a RE-ONBOARD.

    On 2026-08-05 the name went to `PA-VM`, the device-scope interface overrides
    were destroyed, and `is_first_push_done` returned to false. A push in that
    window would have stripped the addressing off a working firewall. The name is
    the cheapest visible signal that it happened.
    """
    cat = CATALOG.replace(
        '        display_name: fw-a\n', '        display_name: fw-a\n')
    rows = [dict(r) for r in LIVE_OK]
    for r in rows:
        if r["name"] == "007955000894453":
            r["display_name"] = "PA-VM"          # what a re-onboard leaves behind
    findings = compare(_h(cat), _live(rows))
    hits = [f for f in findings if "display_name" in f.message]
    assert len(hits) == 1
    assert not hits[0].blocking, "a label must not fail the pipeline"
    assert "RE-ONBOARDED" in hits[0].message


def test_a_matching_display_name_is_silent():
    """A check that fires when nothing is wrong is one people stop reading."""
    cat = CATALOG.replace(
        '        display_name: fw-a\n', '        display_name: fw-a\n')
    rows = [dict(r) for r in LIVE_OK]
    for r in rows:
        if r["name"] == "007955000894453":
            r["display_name"] = "fw-a"
    assert not [f for f in compare(_h(cat), _live(rows)) if "display_name" in f.message]


def test_a_device_with_no_declared_display_name_is_not_reported():
    """Declaring one is optional. Absence means "not tracked", not "mismatched" —
    otherwise adding the check would have flagged every device in every catalog
    that had not opted in yet."""
    bare = CATALOG.replace("        display_name: fw-a\n", "")
    rows = [dict(r) for r in LIVE_OK]
    for r in rows:
        if r["name"] == "007955000894453":
            r["display_name"] = "anything-at-all"
    assert not [f for f in compare(_h(bare), _live(rows)) if "display_name" in f.message]
