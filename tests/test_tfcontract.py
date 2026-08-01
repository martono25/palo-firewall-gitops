"""The compiler -> Terraform data contract.

These tests exist because the v1.0 suite could not have caught the bug that
shipped: every zone test asserted the compiler wrote the right JSON and stopped
there, which is precisely where the failure lived. Terraform then ignored the
file. So these assert the hop AFTER the compiler.

Two silent holes are covered, and the second is the quieter one:

  HOLE 1  key emitted, no `variable` declared   -> Terraform warns, exits 0
  HOLE 2  variable declared, never passed to the module -> NO diagnostic at all
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.cli import main  # noqa: E402
from fwgitops.tfcontract import (  # noqa: E402
    check_contract,
    declared_variables,
    is_terraform_root,
    module_arguments,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CLEAN_MODULE = '''
variable "folder" { type = string }
variable "zones"  { type = any }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
  zones  = var.zones
}
'''

# `zones` is declared but never passed to the module block. Terraform says
# NOTHING about this — quieter than the undeclared case.
UNWIRED_MODULE = '''
variable "folder" { type = string }
variable "zones"  { type = any }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
}
'''

UNDECLARED_MODULE = '''
variable "folder" { type = string }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
}
'''


def _root(tmp_path: Path, body: str) -> Path:
    (tmp_path / "main.tf").write_text(body, encoding="utf-8")
    return tmp_path


# ── parsing ───────────────────────────────────────────────────────────────
def test_declared_variables_and_module_arguments(tmp_path):
    d = _root(tmp_path, CLEAN_MODULE)
    assert declared_variables(d) == {"folder", "zones"}
    assert {"folder", "zones"} <= module_arguments(d)


def test_commented_out_declaration_does_not_count(tmp_path):
    d = _root(tmp_path, '# variable "zones" { type = any }\nvariable "folder" {}\n')
    assert declared_variables(d) == {"folder"}


def test_nested_object_keys_are_not_module_arguments(tmp_path):
    """A key inside an object VALUE must not be mistaken for a module argument."""
    d = _root(
        tmp_path,
        '''
variable "folder" {}
module "m" {
  source = "../x"
  folder = var.folder
  settings = {
    zones = "not-a-module-argument"
  }
}
''',
    )
    args = module_arguments(d)
    assert "folder" in args and "settings" in args
    assert "zones" not in args


# ── the two holes ─────────────────────────────────────────────────────────
def test_clean_module_has_no_violations(tmp_path):
    assert check_contract(_root(tmp_path, CLEAN_MODULE), ["zones"]) == []


def test_hole_1_undeclared_variable_is_a_violation(tmp_path):
    problems = check_contract(_root(tmp_path, UNDECLARED_MODULE), ["zones"])
    assert len(problems) == 1
    assert "no `variable \"zones\"`" in problems[0]


def test_hole_2_declared_but_unwired_variable_is_a_violation(tmp_path):
    problems = check_contract(_root(tmp_path, UNWIRED_MODULE), ["zones"])
    assert len(problems) == 1
    assert "never passed to a module block" in problems[0]


def test_emitting_into_a_non_terraform_directory_is_a_violation(tmp_path):
    problems = check_contract(tmp_path, ["zones"])
    assert len(problems) == 1
    assert "no Terraform root module" in problems[0]


def test_no_emitted_keys_is_vacuously_fine(tmp_path):
    assert check_contract(tmp_path, []) == []
    assert not is_terraform_root(tmp_path)


# ── end-to-end: the compiler must refuse to write orphan data ─────────────
ENV_MAP = "prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n"

ZONE_INTENT = (
    "apiVersion: fw-intent/v1\n"
    "kind: ZoneRequest\n"
    "metadata: {id: ZONE-1, requester: m@corp, ticket: J-1, justification: dmz,"
    " requested: 2026-07-27}\n"
    "spec: {environment: prod, zone: dmz, type: layer3, interfaces: [ethernet1/2]}\n"
)


@pytest.mark.parametrize(
    "module_body,expected",
    [(UNDECLARED_MODULE, "no `variable"), (UNWIRED_MODULE, "never passed to a module block")],
)
def test_compile_refuses_to_write_orphan_tfvars(tmp_path, capsys, module_body, expected):
    """THE REGRESSION TEST for the v1.0 bug.

    A ZoneRequest compiled into a folder whose module cannot consume `zones`
    must FAIL and write nothing — not exit 0 having produced a file Terraform
    will ignore.
    """
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")

    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    folder_dir = tmp_path / "terraform" / "prod-edge"
    folder_dir.mkdir(parents=True)
    (folder_dir / "main.tf").write_text(module_body, encoding="utf-8")

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map),
        "--out", str(tmp_path / "terraform"),
    ])

    assert rc == 2, "compile must fail closed, not silently emit ignored data"
    assert expected in capsys.readouterr().err
    assert not (folder_dir / "zones.auto.tfvars.json").exists(), "nothing may be written"


def test_compile_succeeds_when_the_module_consumes_zones(tmp_path):
    """Same intent, module wired correctly -> writes normally."""
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")

    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    folder_dir = tmp_path / "terraform" / "prod-edge"
    folder_dir.mkdir(parents=True)
    (folder_dir / "main.tf").write_text(CLEAN_MODULE, encoding="utf-8")

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map),
        "--out", str(tmp_path / "terraform"),
    ])

    assert rc == 0
    payload = json.loads((folder_dir / "zones.auto.tfvars.json").read_text())
    assert payload["zones"]["dmz"]["network"] == {"layer3": ["ethernet1/2"]}


# ── live repo contract ────────────────────────────────────────────────────
def test_this_repos_terraform_roots_satisfy_the_contract():
    """Guards the real tree: every root module must consume what it declares.

    Catches the next kind wired into the compiler but not into Terraform.
    """
    roots = [
        d
        for d in (REPO_ROOT / "terraform").iterdir()
        if d.is_dir()
        and is_terraform_root(d)
        and d.name not in {"modules"}
        and not d.name.startswith("bootstrap-")
        and d.name != "github-oidc"
    ]
    assert roots, "expected at least one Terraform root module"

    for root in roots:
        unwired = sorted(declared_variables(root) - module_arguments(root))
        assert not unwired, (
            f"{root.name}: variable(s) {unwired} declared but never passed to a module "
            f"block — Terraform gives no diagnostic and the value is silently ignored"
        )
