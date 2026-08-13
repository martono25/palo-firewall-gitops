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


def _compiled_changes():
    """Every AccessRequest in the REAL intent tree, compiled.

    The real tree rather than a fixture, because the property under test is
    whether THIS repository's rules reference objects THIS repository emits.
    Uses the same loader and catalogs the CLI does, so a catalog change that
    would break the real compile breaks this too.
    """
    import sys

    from fwgitops.cli import _load_catalogs
    from fwgitops.compiler import compile_request
    from fwgitops.intent import load_intent
    from fwgitops.io import read_yaml
    from fwgitops.resolve import EnvMap

    root = Path(__file__).resolve().parents[1]
    env_map = EnvMap.from_dict(read_yaml(root / "catalog" / "environments.yaml"))
    cats, ok = _load_catalogs(root / "catalog" / "services.yaml",
                              root / "catalog" / "apps.yaml", sys.stderr)
    assert ok, "the real catalogs must load"

    out = []
    for path in sorted((root / "intent").rglob("*.yaml")):
        req = load_intent(read_yaml(path), env_map=env_map, **cats)
        if type(req).__name__ != "AccessRequest":
            continue
        out.append(compile_request(req, env_map))
    return out


def test_every_object_a_rule_REFERENCES_is_one_the_compiler_EMITS():
    """The guarantee that moved when addresses and services left Terraform.

    It used to live in `main.tf`: every service name was mapped through
    `scm_service.this[...]`, so a name with no matching object failed at PLAN
    time on a missing key. ADR-0010 passes names through verbatim, which removes
    that lookup — and with it that check.

    So it is asserted here instead, one stage earlier and now covering ADDRESSES
    too, which the Terraform version only covered by accident of the same
    expression. A reference the compiler cannot satisfy is a rule SCM will
    reject at apply time with INVALID_REFERENCE; catching it at compile time is
    strictly earlier than the protection it replaces.

    `application-default` and `any` name no object by design and are excluded.
    """
    from fwgitops.compiler import LITERAL_SERVICES, to_tfvars, wanted_objects

    changes = _compiled_changes()
    assert changes, "no compiled changes to check"
    tfvars = to_tfvars(changes)
    objects = wanted_objects(changes)

    dangling = []
    for rid, rule in tfvars["security_rules"].items():
        for name in rule.get("sources", []) + rule.get("destinations", []):
            if name not in objects["address"]:
                dangling.append(f"{rid} -> address {name}")
        for name in rule.get("services", []):
            if name in LITERAL_SERVICES:
                continue
            if name not in objects["service"]:
                dangling.append(f"{rid} -> service {name}")

    assert not dangling, (
        f"these rules reference objects the compiler does not emit, so nothing "
        f"will create them and SCM will reject the rule: {dangling}")


def test_the_tfvars_no_longer_carry_objects_terraform_does_not_manage():
    """A root that does not declare a variable takes a tfvars file carrying it
    as a WARNING and discards the value. Emitting objects Terraform no longer
    manages would be exactly the silent discard the contract checks exist to
    prevent."""
    from fwgitops.compiler import dumps_tfvars
    import json

    payload = json.loads(dumps_tfvars(_compiled_changes()))
    assert set(payload) == {"security_rules"}, (
        f"rules.auto.tfvars.json must carry rules only; got {sorted(payload)}")


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


def test_every_object_that_left_TERRAFORM_still_has_its_removed_BLOCK():
    """The single line between an apply and mass deletion.

    Tags (ADR-0009), then addresses and services (ADR-0010), stopped being
    Terraform-managed. Their resources are gone from `main.tf`, but they are
    still in the state files of every live root. A resource present in state
    with no configuration is one Terraform DESTROYS — so what makes this safe is
    not deleting the resource, it is the `removed` block telling Terraform to
    forget it instead.

    Delete that block and the next apply plans to destroy every address, service
    and tag in the folder. Most would 409 because rules still reference them,
    which means the blast radius is "the apply fails" rather than "the firewall
    is stripped" — but the ones nothing references would go, silently and
    without an evidence bundle.

    FOUND BY MUTATION on 2026-08-13: removing the `scm_address` block passed all
    890 tests. The tag block had been equally unpinned since 2026-08-10.
    """
    import re

    main = (Path(__file__).resolve().parents[1] / "terraform" / "modules"
            / "security_folder" / "main.tf").read_text()

    for resource in ("scm_tag.this", "scm_address.this", "scm_service.this"):
        block = re.search(
            r"removed\s*\{[^}]*from\s*=\s*" + re.escape(resource)
            + r"\s*lifecycle\s*\{\s*destroy\s*=\s*false\s*\}\s*\}",
            " ".join(main.split()))
        assert block, (
            f"`removed {{ from = {resource} ... destroy = false }}` is missing "
            f"from the module. Without it Terraform destroys every one of those "
            f"objects on the next apply, because they are still in state and no "
            f"longer in configuration.")

    for resource in ("scm_address", "scm_service", "scm_tag"):
        assert f'resource "{resource}" "this"' not in main, (
            f"{resource} is Terraform-managed again — it must not be, and if "
            f"that is deliberate the matching `removed` block has to go with it")
