"""Re-attaching lost Terraform state to the SCM objects Git declares.

The rows below are the shapes SCM returned on 2026-09-14/15, and the two id forms
are copied from live `tfid` values — the provider docs show placeholders, and a
placeholder read as a literal is how an import silently targets nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from fwgitops.cli import run_recover_state
from fwgitops.recover import (
    FILENAME, Scope, declared_objects, import_blocks, plan_is_safe,
)

SERIAL = "007955000919340"


def _rule(name, uuid, folder="prod-edge"):
    return {"name": name, "id": uuid, "folder": folder}


# ── ids ─────────────────────────────────────────────────────────────────────

def test_the_FOLDER_import_id_matches_live_state():
    """`tfid` on REQ-2026-0725 in prod-edge state, 2026-09-14."""
    assert Scope("folder", "prod-edge").import_id("392ba2d1") == "prod-edge:::392ba2d1"


def test_the_DEVICE_import_id_matches_live_state():
    """`tfid` on ethernet1/1 in device-007955000919340 state, 2026-09-15. The docs
    write `::device:id`; `device` there is the SERIAL, not the word."""
    assert Scope("device", SERIAL).import_id("be4ac066") == f"::{SERIAL}:be4ac066"


def test_a_root_directory_names_its_scope():
    assert Scope.of_root(Path("terraform/prod-edge")) == Scope("folder", "prod-edge")
    assert Scope.of_root(Path(f"terraform/device-{SERIAL}")) == Scope("device", SERIAL)


# ── what is imported ────────────────────────────────────────────────────────

def test_only_DECLARED_objects_are_imported_never_what_SCM_happens_to_hold():
    """STATE RECOVERY, NOT ADOPTION (ADR-0011). A hand-made rule in the same
    folder is live and has an id; importing it would make it managed by a Git
    repository that never declared it."""
    live = {"security_rules": [_rule("REQ-2026-0725", "a"), _rule("console-hack", "b")]}
    rec = import_blocks(Scope("folder", "prod-edge"),
                        {"security_rules": {"REQ-2026-0725": {"name": "REQ-2026-0725"}}}, live)
    text = rec.render()
    assert 'this["REQ-2026-0725"]' in text and '"prod-edge:::a"' in text
    assert "console-hack" not in text and ":::b" not in text


def test_an_INHERITED_object_is_never_imported_into_a_child():
    """A list call returns ancestors' objects too. Importing `ngfw-shared`'s
    `$eth-local` into prod-edge would put the parent's object in the child's
    state — and a later destroy there would delete it for every sibling."""
    live = {"folder_interfaces": [{"name": "$eth-dmz", "id": "inherited", "folder": "ngfw-shared"}]}
    rec = import_blocks(Scope("folder", "prod-edge"),
                        {"folder_interfaces": {"$eth-dmz": {"name": "$eth-dmz"}}}, live)
    assert rec.blocks == [] and rec.missing == ['scm_ethernet_interface["$eth-dmz"]']


def test_a_device_root_matches_on_the_DEVICE_field():
    live = {"interfaces": [{"name": "ethernet1/1", "id": "be4ac066", "device": SERIAL,
                            "folder": None}]}
    rec = import_blocks(Scope("device", SERIAL),
                        {"interfaces": {"ethernet1/1": {"name": "ethernet1/1"}}}, live)
    assert f'id = "::{SERIAL}:be4ac066"' in rec.render()


def test_a_declared_object_absent_from_SCM_is_reported_MISSING():
    rec = import_blocks(Scope("folder", "prod-edge"),
                        {"zones": {"dmz": {"name": "dmz"}}}, {"zones": []})
    assert rec.missing == ['scm_zone["dmz"]'] and rec.blocks == []


def test_declared_objects_are_read_from_EVERY_compiled_file(tmp_path):
    (tmp_path / "rules.auto.tfvars.json").write_text(json.dumps(
        {"security_rules": {"R1": {"name": "R1"}}}))
    (tmp_path / "zones.auto.tfvars.json").write_text(json.dumps({"zones": {"dmz": {}}}))
    (tmp_path / "interface_vars.auto.tfvars.json").write_text(json.dumps(
        {"folder_interfaces": {"$eth-dmz": {"name": "$eth-dmz"}}}))
    assert sorted(declared_objects(tmp_path)) == ["folder_interfaces", "security_rules", "zones"]


# ── the plan gate ───────────────────────────────────────────────────────────

def test_the_plan_gate_accepts_import_artifacts_but_not_creates_or_destroys():
    """The 2026-09-14 recovery plan, verbatim, passes: the in-place changes are
    `position = "pre"`, which an import id cannot carry."""
    assert plan_is_safe("Plan: 9 to import, 0 to add, 6 to change, 0 to destroy.")
    assert plan_is_safe("No changes. Your infrastructure matches the configuration.")
    assert not plan_is_safe("Plan: 8 to import, 1 to add, 6 to change, 0 to destroy.")
    assert not plan_is_safe("Plan: 9 to import, 0 to add, 0 to change, 1 to destroy.")
    assert not plan_is_safe(None), "an unreadable plan is not a safe one"


# ── the command ─────────────────────────────────────────────────────────────

class _Scm:
    def __init__(self, rows):
        self.rows = rows

    def request(self, method, path, params=None, body=None):
        return {"data": [r for r in self.rows.get(path, [])
                         if all(r.get(k) == v for k, v in (params or {}).items()
                                if k in ("folder", "device"))]}


def _root(tmp_path, name, tfvars):
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "main.tf").write_text('module "security_folder" {}\n')
    (root / "rules.auto.tfvars.json").write_text(json.dumps(tfvars))
    return root


def test_the_command_writes_nothing_when_ANY_object_is_missing(tmp_path, capsys):
    """A partial recovery is worse than none: the file looks like a recovery,
    and the next apply CREATES what it left out."""
    ok = _root(tmp_path, "GitOps", {"security_rules": {"R1": {"name": "R1"}}})
    bad = _root(tmp_path, "prod-edge", {"security_rules": {"R2": {"name": "R2"}}})
    scm = _Scm({"/config/security/v1/security-rules": [_rule("R1", "x", folder="GitOps")]})
    assert run_recover_state(tmp_path, session=scm) == 2
    assert not (ok / FILENAME).exists() and not (bad / FILENAME).exists()
    assert "would CREATE" in capsys.readouterr().err


def test_the_command_writes_one_file_per_root_when_complete(tmp_path):
    root = _root(tmp_path, "prod-edge", {"security_rules": {"R2": {"name": "R2"}}})
    scm = _Scm({"/config/security/v1/security-rules": [_rule("R2", "uuid-2")]})
    assert run_recover_state(tmp_path, session=scm) == 0
    assert '"prod-edge:::uuid-2"' in (root / FILENAME).read_text()


def test_CHECK_writes_nothing(tmp_path):
    root = _root(tmp_path, "prod-edge", {"security_rules": {"R2": {"name": "R2"}}})
    scm = _Scm({"/config/security/v1/security-rules": [_rule("R2", "uuid-2")]})
    assert run_recover_state(tmp_path, session=scm, check=True) == 0
    assert not (root / FILENAME).exists()


def test_bootstrap_roots_are_never_recovery_targets(tmp_path):
    """bootstrap-backend and github-oidc hold AWS resources in LOCAL state; they
    are recreated, not imported."""
    _root(tmp_path, "bootstrap-backend", {"security_rules": {"R": {"name": "R"}}})
    assert run_recover_state(tmp_path, session=_Scm({})) == 1


def test_a_DEAD_firewall_is_skipped_with_what_to_do_not_counted_missing(tmp_path, capsys):
    """On an account move the firewall dies with the account. Its interfaces are
    not in SCM because the device is not — that is retirement, not a partial
    recovery, and it must not block the folders from being recovered."""
    folder = _root(tmp_path, "prod-edge", {"security_rules": {"R2": {"name": "R2"}}})
    dead = _root(tmp_path, "device-007955000902404",
                 {"interfaces": {"ethernet1/1": {"name": "ethernet1/1"}}})
    scm = _Scm({"/config/security/v1/security-rules": [_rule("R2", "uuid-2")],
                "/config/setup/v1/devices": []})
    assert run_recover_state(tmp_path, session=scm) == 0
    assert (folder / FILENAME).exists() and not (dead / FILENAME).exists()
    assert "Retire it" in capsys.readouterr().out


def test_a_LIVE_firewall_whose_interfaces_are_missing_still_refuses(tmp_path):
    """The skip is for an ABSENT device only. A registered firewall missing a
    declared interface is a real gap, and the apply would create it."""
    _root(tmp_path, f"device-{SERIAL}", {"interfaces": {"ethernet1/1": {"name": "ethernet1/1"}}})
    scm = _Scm({"/config/setup/v1/devices": [{"serial_number": SERIAL}],
                "/config/network/v1/ethernet-interfaces": []})
    assert run_recover_state(tmp_path, session=scm) == 2
