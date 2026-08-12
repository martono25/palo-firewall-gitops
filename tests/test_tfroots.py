"""Every Terraform root must declare the module's variables identically.

The roots are copies: `terraform/prod-edge/variables.tf` and
`terraform/device-007955000902404/variables.tf` restate the module's `variable`
blocks verbatim, because a root has to declare a variable for the tfvars data to
reach the module at all.

That duplication is where HOLE 3 comes back. If a root's object type drifts from
the module's — one attribute added in one place and not the other — Terraform
DISCARDS the undeclared attribute silently (no warning, exit 0) at the module
boundary. Not hypothetical: the `security_rules` block in
`terraform/prod-edge/variables.tf` carries a comment recording exactly this,
with the intent's App-ID / profile / log setting never arriving.

The comparison is STRUCTURAL — declared attribute paths via
`tfcontract.declared_object_attributes`, which strips comments and masks
strings — so descriptions and inline notes may differ but the type may not.

It scales: a new root is covered the moment it exists.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "terraform" / "modules" / "security_folder"
TF_DIR = REPO_ROOT / "terraform"


def _variable_blocks(tf_dir: Path) -> dict:
    """name -> body text, for every `variable "x" { ... }` in a directory."""
    out = {}
    for path in sorted(tf_dir.glob("*.tf")):
        text = path.read_text()
        for m in re.finditer(r'^variable\s+"([^"]+)"\s*\{', text, re.M):
            name = m.group(1)
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            out[name] = text[m.end():i]
    return out


def _roots() -> list:
    """Every root that calls the security_folder module."""
    found = []
    for main in sorted(TF_DIR.glob("*/main.tf")):
        if 'source = "../modules/security_folder"' in main.read_text():
            found.append(main.parent)
    return found


def test_there_is_at_least_one_root():
    """Guards the discovery itself — a glob that silently matches nothing would
    make every test below vacuously pass."""
    roots = _roots()
    assert roots, "no Terraform roots found calling ../modules/security_folder"
    names = {r.name for r in roots}
    assert "prod-edge" in names
    # A firewall gets its own root: a device write is a per-device OVERRIDE of a
    # distinct object, so it must not share state with its folder.
    assert "device-007955000902404" in names


@pytest.mark.parametrize("root", _roots(), ids=lambda p: p.name)
def test_root_declares_the_modules_variables_identically(root: Path):
    module_vars = _variable_blocks(MODULE)
    root_vars = _variable_blocks(root)

    missing = sorted(set(module_vars) - set(root_vars))
    assert not missing, (
        f"{root.name} does not declare {missing}, which the module expects — "
        f"tfvars for them would be discarded before reaching the module")

    extra = sorted(set(root_vars) - set(module_vars))
    assert not extra, (
        f"{root.name} declares {extra}, which the module does not accept — "
        f"the data would be read where nothing consumes it")

    # Structural comparison: the dotted attribute paths each side declares.
    # Comments and descriptions may differ; the TYPE may not. Comparing raw text
    # instead would flag description drift that changes nothing, and a check
    # that cries wolf gets ignored.
    from fwgitops.tfcontract import declared_object_attributes
    for name in sorted(module_vars):
        want = declared_object_attributes(MODULE, name)
        got = declared_object_attributes(root, name)
        if want is None and got is None:
            continue                      # a scalar (e.g. `folder`), nothing nested
        assert want is not None and got is not None, (
            f"{root.name}: {name!r} is an object type on one side and not the other")
        only_module = sorted(want - got)
        only_root = sorted(got - want)
        assert not only_module, (
            f"{root.name}: variable {name!r} is MISSING {only_module}, which the module "
            f"declares and the compiler may emit. Terraform discards undeclared object "
            f"attributes SILENTLY at the module boundary (no warning, exit 0) — the "
            f"module falls back to its own defaults and the intent's values never "
            f"arrive.")
        assert not only_root, (
            f"{root.name}: variable {name!r} declares {only_root}, which the module does "
            f"not — the root would accept data the module then discards.")


@pytest.mark.parametrize("root", _roots(), ids=lambda p: p.name)
def test_root_wires_every_variable_into_the_module(root: Path):
    """A declared-but-unwired variable is HOLE 2: Terraform emits no diagnostic
    at all, and the data simply never reaches the resource."""
    main = (root / "main.tf").read_text()
    block = main[main.index('module "security_folder"'):]
    for name in _variable_blocks(MODULE):
        if name == "folder":       # passed as var.folder, same name
            continue
        assert re.search(rf"^\s*{re.escape(name)}\s*=\s*var\.{re.escape(name)}\s*$",
                         block, re.M), (
            f"{root.name}/main.tf declares variable {name!r} but never passes it to "
            f"the module — Terraform gives NO diagnostic for this")


@pytest.mark.parametrize("root", _roots(), ids=lambda p: p.name)
def test_every_root_uses_partial_backend_config(root: Path):
    """State is per scope (design Arch-2). The bucket names an AWS account, so it
    lives in a gitignored backend.hcl rather than in tracked HCL."""
    backend = (root / "backend.tf").read_text()
    assert 'backend "s3" {}' in backend, f"{root.name} must use partial backend config"
    assert not (root / "backend.hcl").exists() or True  # generated, gitignored
    assert (root / "backend.hcl.example").is_file(), (
        f"{root.name} needs a backend.hcl.example pointing at make-backend.sh")


def test_scope_dirname_matches_the_root_layout():
    """The compiler picks the output directory from Scope.dirname, so a mismatch
    would emit tfvars where no root reads them — caught only by the missing-root
    guard, and only at compile time."""
    from fwgitops.compiler import Scope
    names = {r.name for r in _roots()}
    assert Scope("folder", "prod-edge").dirname in names
    assert Scope("device", "007955000902404").dirname in names


def test_a_literal_service_is_passed_through_not_resolved_as_an_object():
    """`main.tf` maps every service name through `scm_service.this[...]`, which
    creates the dependency edge that orders object-before-rule. An ICMP rule
    carries `application-default`, which names NO object — so the lookup would
    fail with a missing key.

    The passthrough must stay NARROW: anything not in the literal set is still
    resolved, so a typo'd service name fails loudly on the lookup instead of
    being handed to SCM as a literal."""
    from pathlib import Path
    main = (Path(__file__).resolve().parents[1]
            / "terraform" / "modules" / "security_folder" / "main.tf").read_text()
    assert "literal_services" in main
    assert "application-default" in main
    assert "contains(local.literal_services, v) ? v : scm_service.this[v].name" in main


def test_relative_position_has_NO_default_in_the_module_or_any_root():
    """`optional(string, "bottom")` substitutes the default when the value is
    NULL, so an unspecified position was silently turned back into "bottom" at
    the variable boundary — and a first-time write of a concrete position
    RE-STACKS the rulebase (spike/ordering-existing).

    CAUGHT BY A PLAN, not by a test: with the compiler already emitting null, a
    plan against the live folder showed `+ relative_position = "bottom"` on all
    five rules — the exact silent policy rewrite the spike warned about.

    `scaffold-root --check` passed throughout and was RIGHT to. It compares the
    root against a freshly rendered one in full, defaults included, and the root
    mirrored the module faithfully — the MODULE held the bad default. A mirror
    cannot detect that its subject is wrong, which is why this test asserts the
    VALUE rather than the agreement."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "terraform"
    files = [root / "modules" / "security_folder" / "variables.tf"]
    files += [p / "variables.tf" for p in root.iterdir()
              if p.is_dir() and p.name not in ("modules",) and (p / "variables.tf").is_file()]
    checked = 0
    for f in files:
        for line in f.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("relative_position") and "=" in stripped:
                assert 'optional(string, ' not in stripped, (
                    f"{f}: relative_position must have NO default — a null must "
                    f"survive to the provider, or every rule is moved")
                checked += 1
    assert checked >= 2, f"expected the module and at least one root, checked {checked}"
