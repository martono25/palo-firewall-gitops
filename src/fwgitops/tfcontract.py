"""The compiler → Terraform data contract, checked in Python.

The compiler emits `*.auto.tfvars.json`; a Terraform root module is supposed to
consume it. Nothing enforced that, and there are TWO ways it silently fails:

    intent -> compile -> *.auto.tfvars.json
                              |
                    HOLE 1: variables.tf declares no such variable
                            -> Terraform WARNS and exits 0. Ignored.
                              |
                    HOLE 2: variable declared but never passed to the module
                            -> NO diagnostic at all. Ignored.
                              |
                         module -> resource -> SCM -> device

Hole 1 shipped a whole release: `zones.auto.tfvars.json` was written on every
compile, plan and apply stayed green, and the zone never reached the firewall.
Hole 2 is quieter still — Terraform says nothing whatsoever.

Both are cheap to check without a Terraform binary or cloud credentials, so they
are checked here: at compile time (fail-closed, see `cli.py`) and in the test
suite (`tests/test_tfcontract.py`).

Deliberately a small regex/brace parser rather than a full HCL parser: the inputs
are this repo's own hand-written root modules, and adding an HCL dependency to
catch a malformed file we would notice anyway is not worth it. Comments are
stripped first so a commented-out `variable "x"` never counts as declared.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Set

#: `variable "name" {`
_VARIABLE_RE = re.compile(r'^\s*variable\s+"([^"]+)"\s*\{', re.MULTILINE)
#: `module "name" {`
_MODULE_RE = re.compile(r'^\s*module\s+"([^"]+)"\s*\{', re.MULTILINE)
#: `name = ...` at the start of a line (a module-block argument)
_ARG_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', re.MULTILINE)


def _strip_comments(text: str) -> str:
    """Remove `#` and `//` line comments so commented-out HCL never counts.

    Good enough for this repo's modules: no `#` appears inside a string literal
    in any of them. Block comments (/* */) are not used either.
    """
    out: List[str] = []
    for line in text.splitlines():
        for marker in ("#", "//"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _read_tf(module_dir: Path) -> str:
    """Concatenate every .tf file in a root module directory (comments stripped)."""
    parts = [p.read_text(encoding="utf-8") for p in sorted(module_dir.glob("*.tf"))]
    return _strip_comments("\n".join(parts))


def is_terraform_root(module_dir: Path) -> bool:
    """True when the directory actually holds a Terraform root module."""
    return module_dir.is_dir() and any(module_dir.glob("*.tf"))


def declared_variables(module_dir: Path) -> Set[str]:
    """Variable names declared anywhere in the root module (HOLE 1 input)."""
    return set(_VARIABLE_RE.findall(_read_tf(module_dir)))


def module_arguments(module_dir: Path) -> Set[str]:
    """Argument names passed to any `module "..."` block (HOLE 2 input).

    Brace-matched so only top-level arguments of the module block count, not
    keys nested inside an object value passed to one.
    """
    text = _read_tf(module_dir)
    args: Set[str] = set()
    for match in _MODULE_RE.finditer(text):
        depth, i, n = 0, match.end() - 1, len(text)
        start = match.end()
        while i < n:  # find the matching closing brace
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[start:i]
        # Only depth-1 lines are module arguments; skip nested object contents.
        depth = 0
        for line in body.splitlines():
            if depth == 0:
                found = _ARG_RE.match(line)
                if found:
                    args.add(found.group(1))
            depth += line.count("{") + line.count("[")
            depth -= line.count("}") + line.count("]")
    return args


def check_contract(module_dir: Path, emitted_keys: Iterable[str]) -> List[str]:
    """Verify emitted tfvars keys are both DECLARED and WIRED. Fail-closed.

    Returns actionable violation messages; an empty list means the contract holds.
    Checking only the declaration would still let HOLE 2 through.
    """
    keys = sorted(set(emitted_keys))
    if not keys:
        return []
    if not is_terraform_root(module_dir):
        return [
            f"{module_dir}: no Terraform root module here, but the compiler emitted "
            f"{keys} — the data would be written where nothing reads it"
        ]

    declared = declared_variables(module_dir)
    wired = module_arguments(module_dir)

    violations: List[str] = []
    for key in keys:
        if key not in declared:
            violations.append(
                f"{module_dir}: compiled data sets {key!r} but no `variable \"{key}\"` is "
                f"declared — Terraform would WARN and silently ignore it (exit 0). "
                f"Add it to variables.tf and pass it to the module block."
            )
        elif key not in wired:
            violations.append(
                f"{module_dir}: `variable \"{key}\"` is declared but never passed to a "
                f"module block — Terraform gives NO diagnostic and the data is silently "
                f"ignored. Add `{key} = var.{key}` to the module block."
            )
    return violations


def emitted_keys_by_folder(files: Dict[str, Dict[str, object]]) -> Dict[str, Set[str]]:
    """Collapse {folder: tfvars-payload} into {folder: top-level key names}."""
    return {folder: set(payload.keys()) for folder, payload in files.items()}
