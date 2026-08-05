"""Generate a Terraform ROOT for an SCM scope, from the module it calls.

WHY THIS EXISTS. A root is almost entirely boilerplate that must track the
module EXACTLY. `terraform/prod-edge/variables.tf` and
`terraform/device-.../variables.tf` are byte-identical today, and both must
mirror `modules/security_folder/variables.tf` attribute for attribute — because
Terraform DISCARDS an undeclared object attribute at the module boundary
silently, with no warning and exit 0 (ADR-0004, HOLE 3). A root that drifts does
not fail; it quietly stops delivering part of every intent.

Two costs came out of that, and this module exists to remove both:

  1. A NEW FOLDER could not be created without hand-copying ~260 lines and
     getting every nested attribute right. That was the last manual step between
     "add a folder to the catalog" and a working firewall, and the reason
     greenfield was still not true after `fwgitops folder-interfaces`.

  2. ADDING A MODULE VARIABLE broke every existing root. It happened on
     2026-08-05: the module gained `folder_interfaces` and the device root, which
     has no use for it, failed the contract test — correctly, because a root that
     omits a variable IS the hole. The tests DETECT that; generation prevents it.

So the roots are derived, and `--check` asserts they still are. The contract
tests stay exactly as they were: they are the independent check that generation
did the right thing, and a generator marking its own homework is worth little.

WHAT IS NOT GENERATED. `main.tf` carries hand-written reasoning that matters (why
a device root is separate from its folder's, what a device-scope write actually
does). It is scaffolded ONCE for a new root and never overwritten. Its module
block is still covered by `test_root_wires_every_variable_into_the_module`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MODULE_SOURCE = "../modules/security_folder"


class ScaffoldError(Exception):
    """A root cannot be generated. Always fail rather than emit a partial root."""


@dataclass(frozen=True)
class Scope:
    """What a root is FOR. Exactly one of folder/device, like every scope here."""

    folder: Optional[str] = None
    device: Optional[str] = None

    @property
    def dirname(self) -> str:
        return self.folder if self.folder else f"device-{self.device}"

    @property
    def scm_folder_value(self) -> str:
        """The value of the root's `folder` variable.

        For a DEVICE root this is still the containing folder: the module scopes
        its `scm_tag` objects with it, and a tag is a folder object even when the
        interface overriding it is a device one. A serial here would be wrong in
        the way SCM punishes — `folder=<serial>` returns "Folder doesn't exist".
        """
        if self.folder:
            return self.folder
        raise ScaffoldError(
            "a device root needs its CONTAINING folder for the `folder` variable "
            "(tags are folder objects even when the interface is a device override); "
            "pass it explicitly")


def variable_blocks(tf_dir: Path) -> Dict[str, str]:
    """name -> full `variable "x" { ... }` text, for every .tf in a directory."""
    out: Dict[str, str] = {}
    for path in sorted(tf_dir.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
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
            if depth != 0:
                raise ScaffoldError(f"unbalanced braces in {path} for variable {name!r}")
            out[name] = text[m.start():i + 1]
    return out


def _strip_default(block: str) -> str:
    """Remove a top-level `default = ...` so the root's own can be set.

    Only a default at brace depth 1 is a variable default; one deeper belongs to
    an `optional(...)` inside the type and must survive untouched.
    """
    lines = block.splitlines()
    out: List[str] = []
    depth = 0
    for line in lines:
        stripped = line.strip()
        is_default = depth == 1 and re.match(r"^default\s*=", stripped)
        depth += line.count("{") - line.count("}")
        if is_default:
            # A single-line default only. A multi-line one would need brace
            # tracking of its own; none exist, and guessing is how a type gets
            # silently truncated.
            if stripped.count("{") != stripped.count("}"):
                raise ScaffoldError(
                    "multi-line variable default found; the generator only handles "
                    "single-line defaults and will not guess where one ends")
            continue
        out.append(line)
    return "\n".join(out)


def render_variables(module_dir: Path, scope_folder: str) -> str:
    """The root's variables.tf: the module's variables, made auto-loadable.

    Two differences from the module, and only two:

      * every map gets `default = {}`, so `plan` works for a scope with nothing
        compiled into it yet — which is every scope on its first day, and the
        whole point of being able to scaffold one.
      * `folder` gets a default of the scope's SCM folder, because nothing
        auto-loads it.

    The TYPES are copied verbatim. Retyping them by hand is what HOLE 3 is.
    """
    blocks = variable_blocks(module_dir)
    if not blocks:
        raise ScaffoldError(f"no variables found in {module_dir} — wrong directory?")
    if "folder" not in blocks:
        raise ScaffoldError(f"{module_dir} declares no `folder` variable")

    header = (
        "# GENERATED by `fwgitops scaffold-root`. Do not edit by hand.\n"
        "#\n"
        "# These mirror modules/security_folder/variables.tf ATTRIBUTE FOR\n"
        "# ATTRIBUTE. Terraform discards an undeclared object attribute at the\n"
        "# module boundary silently — no warning, exit 0 — so a root that drifts\n"
        "# from the module does not fail, it quietly stops delivering part of\n"
        "# every intent (ADR-0004, HOLE 3).\n"
        "#\n"
        "# Change the MODULE, then run `fwgitops scaffold-root --sync`.\n"
        "# `--check` runs in CI; tests/test_tfroots.py verifies the result\n"
        "# independently, because a generator marking its own homework is worth\n"
        "# little.\n"
        "#\n"
        "# Values arrive from *.auto.tfvars.json, which Terraform auto-loads.\n"
        "# Defaults are empty so `plan` works for a scope with nothing compiled\n"
        "# into it yet — which is every scope on its first day.\n"
    )

    parts: List[str] = [header]
    for name in sorted(blocks):
        block = _strip_default(blocks[name]).rstrip()
        assert block.endswith("}")
        body = block[:-1].rstrip("\n")
        if name == "folder":
            body += f'\n  default     = "{scope_folder}"'
        else:
            body += "\n  default = {}"
        parts.append(body + "\n}")
    return "\n\n".join(parts) + "\n"


def provider_pin(module_dir: Path) -> Tuple[str, str]:
    """The `scm` provider source + version constraint the MODULE pins.

    Read from the module rather than restated, because roots and module drifting
    apart has already broken CI here once: the roots were bumped to
    1.0.12-beta.4 and the module left on `~> 1.0`, which cannot even SELECT that
    version (Terraform excludes pre-releases from range constraints).
    """
    text = (module_dir / "versions.tf").read_text(encoding="utf-8")
    # Scope to the `scm = { ... }` block. A bare `version = "..."` search matches
    # `required_version` first and silently pins the provider to the TERRAFORM
    # constraint — which is a valid-looking string, so nothing downstream
    # complains until an apply picks the wrong provider.
    blk = re.search(r"scm\s*=\s*\{(.*?)\n\s*\}", text, re.S)
    if not blk:
        raise ScaffoldError(
            f"no `scm = {{...}}` provider block in {module_dir/'versions.tf'} — "
            f"refusing to scaffold a root with an unpinned provider")
    src = re.search(r'source\s*=\s*"([^"]+)"', blk.group(1))
    ver = re.search(r'version\s*=\s*"([^"]+)"', blk.group(1))
    if not src or not ver:
        raise ScaffoldError(
            f"cannot read the scm provider pin from {module_dir/'versions.tf'} — "
            f"refusing to scaffold a root with an unpinned provider")
    return src.group(1), ver.group(1)


def render_main(module_dir: Path, scope: Scope, *, header: str) -> str:
    """The root's main.tf. Scaffolded ONCE; never regenerated over prose."""
    names = sorted(variable_blocks(module_dir))
    width = max(len(n) for n in names)
    wiring = "\n".join(f"  {n.ljust(width)} = var.{n}" for n in names)
    source, version = provider_pin(module_dir)
    return (
        f"{header}\n"
        "terraform {\n"
        '  required_version = ">= 1.6"\n'
        "\n"
        "  required_providers {\n"
        "    scm = {\n"
        f'      source  = "{source}"\n'
        f'      version = "{version}" # matches modules/security_folder/versions.tf\n'
        "    }\n"
        "  }\n"
        "}\n"
        "\n"
        'provider "scm" {\n'
        "  # Auth comes from SCM_CLIENT_ID / SCM_CLIENT_SECRET / SCM_SCOPE in the\n"
        "  # environment — the same names `fwgitops` uses. An empty block is\n"
        "  # correct here, not a TODO.\n"
        "}\n"
        "\n"
        'module "security_folder" {\n'
        f'  source = "{MODULE_SOURCE}"\n'
        "\n"
        "  # EVERY module variable is wired. A declared-but-unwired variable is\n"
        "  # HOLE 2: Terraform emits no diagnostic at all and the data simply\n"
        "  # never reaches the resource.\n"
        f"{wiring}\n"
        "}\n"
    )


BACKEND_TF = """# Remote state — ONE state per scope (design Arch-2: S3 + native locking,
# encrypted, never in Git). PARTIAL config: the concrete bucket/region/key live
# in backend.hcl (filled from the bootstrap output), passed at init:
#
#   terraform init -backend-config=backend.hcl
#
# Partial config keeps the bucket name (which contains the account id) out of
# the tracked .tf and lets CI pass the same values.

terraform {
  backend "s3" {}
}
"""


def backend_example(scope: Scope) -> str:
    """Matches the existing roots: point at the generator, do not invite editing.

    backend.hcl itself is gitignored because it names the AWS account.
    """
    return (
        "# Do not hand-edit. Generate this file from the live AWS account:\n"
        f"#   ./terraform/make-backend.sh {scope.dirname}\n"
        "# (backend.hcl is gitignored — it names your account.)\n"
    )
