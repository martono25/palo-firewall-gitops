"""Scaffolding a Terraform ROOT (greenfield: the last manual step).

A root is almost all boilerplate that must mirror the module ATTRIBUTE FOR
ATTRIBUTE, because Terraform discards an undeclared object attribute at the
module boundary silently — no warning, exit 0 (ADR-0004, HOLE 3). Hand-copying
~260 lines and getting every nested attribute right was the last thing between
"add a folder to the catalog" and a working firewall.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.cli import run_scaffold_root  # noqa: E402
from fwgitops.scaffold import (  # noqa: E402
    ScaffoldError, Scope, provider_pin, render_variables, variable_blocks,
)
from fwgitops.tfcontract import declared_object_attributes  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "terraform" / "modules" / "security_folder"


def _scaffold(tmp_path: Path, **kw) -> Path:
    out = tmp_path / "terraform"
    (out / "modules").mkdir(parents=True)
    shutil.copytree(MODULE, out / "modules" / "security_folder")
    rc = run_scaffold_root(out, **kw)
    assert rc == 0
    return out


def test_a_scaffolded_root_mirrors_the_module_attribute_for_attribute(tmp_path):
    """The whole point. A missing NESTED attribute is the dangerous case: the
    root still parses, the plan still runs, and the intent's value is dropped on
    the way into the module."""
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    root = out / "prod-edge-apac"
    module = out / "modules" / "security_folder"

    assert set(variable_blocks(root)) == set(variable_blocks(module))
    for name in variable_blocks(module):
        want = declared_object_attributes(module, name)
        got = declared_object_attributes(root, name)
        assert want == got, f"{name} differs: only-module={want and want - (got or set())}"


def test_every_module_variable_is_wired_into_the_module_block(tmp_path):
    """HOLE 2: a declared-but-unwired variable produces NO Terraform diagnostic
    at all — the data simply never reaches the resource."""
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    main = (out / "prod-edge-apac" / "main.tf").read_text()
    block = main[main.index('module "security_folder"'):]
    for name in variable_blocks(out / "modules" / "security_folder"):
        assert f"{name} = var.{name}" in " ".join(block.split()), \
            f"{name} declared but never passed to the module"


def test_the_provider_pin_comes_from_the_module(tmp_path):
    """Roots and module drifting apart has broken CI here before: roots bumped to
    a pre-release while the module stayed on `~> 1.0`, which cannot even SELECT
    a pre-release. Deriving it means they cannot disagree."""
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    source, version = provider_pin(out / "modules" / "security_folder")
    main = (out / "prod-edge-apac" / "main.tf").read_text()
    assert f'version = "{version}"' in main
    assert f'source  = "{source}"' in main
    # and it must NOT have picked up required_version, which also matches a naive
    # `version = "..."` search and is a valid-looking string
    assert version != ">= 1.6"


def test_scaffolding_never_overwrites_an_existing_root(tmp_path):
    """main.tf carries hand-written reasoning, and a root's backend points at
    real state. Silently regenerating one is how a state file gets orphaned."""
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    marker = "# hand-written reasoning that must survive\n"
    main = out / "prod-edge-apac" / "main.tf"
    main.write_text(marker + main.read_text())

    rc = run_scaffold_root(out, folder="prod-edge-apac")
    assert rc == 1
    assert main.read_text().startswith(marker)


def test_a_device_root_needs_its_containing_folder(tmp_path):
    """A device root's `folder` variable is the CONTAINING folder, not the
    serial: the module scopes its scm_tag objects with it, and tags are folder
    objects even when the interface is a device override. SCM rejects
    `folder=<serial>` outright ("Folder doesn't exist")."""
    out = tmp_path / "terraform"
    (out / "modules").mkdir(parents=True)
    shutil.copytree(MODULE, out / "modules" / "security_folder")

    assert run_scaffold_root(out, device="007955000899999") == 1
    assert not (out / "device-007955000899999").exists()

    assert run_scaffold_root(out, device="007955000899999",
                             device_folder="prod-edge") == 0
    vars_tf = (out / "device-007955000899999" / "variables.tf").read_text()
    assert 'default     = "prod-edge"' in vars_tf
    assert "007955000899999" not in vars_tf


def test_check_detects_a_root_that_drifted_from_the_module(tmp_path):
    """This is the failure that actually happened on 2026-08-05: the module
    gained `folder_interfaces` and every root was suddenly wrong. The tests
    caught it; --check is what catches it BEFORE a human is confused by it."""
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    assert run_scaffold_root(out, check=True) == 0

    module_vars = out / "modules" / "security_folder" / "variables.tf"
    module_vars.write_text(module_vars.read_text() +
                           '\nvariable "brand_new" {\n  type    = map(string)\n'
                           '  default = {}\n}\n')
    assert run_scaffold_root(out, check=True) == 2

    assert run_scaffold_root(out, sync=True) == 0
    assert run_scaffold_root(out, check=True) == 0
    assert 'variable "brand_new"' in (out / "prod-edge-apac" / "variables.tf").read_text()


def test_check_and_sync_do_not_take_a_scope(tmp_path):
    out = _scaffold(tmp_path, folder="prod-edge-apac")
    assert run_scaffold_root(out, check=True, folder="prod-edge-apac") == 1


def test_the_shipped_roots_are_in_sync():
    """Runs against the REAL repo, so a module change that nobody synced fails
    the suite rather than waiting to be discovered by a confusing plan."""
    assert run_scaffold_root(REPO_ROOT / "terraform", check=True) == 0


def test_a_nested_optional_default_is_not_mistaken_for_a_variable_default(tmp_path):
    """`default` appears inside `optional(...)` types too. Stripping one of those
    would silently truncate a TYPE — the exact class of damage this generator
    exists to prevent."""
    out = tmp_path / "terraform"
    mod = out / "modules" / "security_folder"
    mod.mkdir(parents=True)
    (mod / "versions.tf").write_text(
        'terraform {\n  required_version = ">= 1.6"\n  required_providers {\n'
        '    scm = {\n      source  = "PaloAltoNetworks/scm"\n'
        '      version = "1.0.12-beta.4"\n    }\n  }\n}\n')
    (mod / "variables.tf").write_text(
        'variable "folder" {\n  type = string\n}\n\n'
        'variable "thing" {\n  type = map(object({\n'
        '    nested = optional(string, "default-inside-a-type")\n'
        "  }))\n  default = {}\n}\n")
    rendered = render_variables(mod, "prod-edge-apac")
    assert 'optional(string, "default-inside-a-type")' in rendered
    assert rendered.count("default = {}") == 1
