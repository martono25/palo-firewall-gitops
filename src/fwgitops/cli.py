"""fwgitops command-line interface.

    fwgitops compile [intent_root] --env-map catalog/environments.yaml --out terraform

Reads intent YAML, validates + compiles each request, and writes one
`rules.auto.tfvars.json` per SCM folder for the static Terraform module. This is
the entry point the GitHub Actions pipeline calls on a PR.

Fail-closed and all-or-nothing: if ANY intent is invalid or unresolvable, the
aggregated problem report goes to stderr and nothing is written (exit 2). A run
either produces a clean, complete desired-state for every touched folder, or it
produces nothing.

Exit codes:  0 ok · 2 validation/compile error · 1 usage/IO error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fwgitops.compiler import CompiledChange, compile_request, dumps_tfvars
from fwgitops.intent import IntentError, load_intent
from fwgitops.io import discover_intents, read_yaml
from fwgitops.resolve import EnvMap, ResolveError

OUTPUT_FILENAME = "rules.auto.tfvars.json"


def run_compile(
    intent_root: Path,
    env_map_path: Path,
    out_root: Path,
    *,
    write: bool = True,
    out=None,
    err=None,
) -> int:
    """Compile every intent under `intent_root`. Returns a process exit code."""
    # Resolve streams at call time (not import time) so output capture works.
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not env_map_path.is_file():
        print(f"error: env map not found: {env_map_path}", file=err)
        return 1
    try:
        env_map = EnvMap.from_dict(read_yaml(env_map_path))
    except ResolveError as e:
        print(f"error: invalid env map {env_map_path}: {e}", file=err)
        return 1

    if not intent_root.exists():
        print(f"error: intent root not found: {intent_root}", file=err)
        return 1

    intents = discover_intents(intent_root)
    if not intents:
        print(f"no intent files found under {intent_root} (nothing to compile)", file=out)
        return 0

    changes: List[CompiledChange] = []
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001 - report any parse failure per file
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            ar = load_intent(doc)
            changes.append(compile_request(ar, env_map))
        except IntentError as e:
            problems.append(f"{rel}:\n" + "\n".join(f"    {p}" for p in e.problems))
        except ResolveError as e:
            problems.append(f"{rel}: {e}")

    if problems:
        print(
            f"REJECTED — {len(problems)} of {len(intents)} intent file(s) invalid; "
            f"nothing written:",
            file=err,
        )
        for p in problems:
            print(f"  - {p}", file=err)
        return 2

    by_folder = _group_by_folder(changes)
    written: List[Path] = []
    for folder, folder_changes in sorted(by_folder.items()):
        target = out_root / folder / OUTPUT_FILENAME
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dumps_tfvars(folder_changes), encoding="utf-8")
        written.append(target)

    verb = "wrote" if write else "would write"
    print(
        f"OK — compiled {len(changes)} request(s) into {len(written)} folder(s); {verb}:",
        file=out,
    )
    for t in written:
        print(f"  - {t}", file=out)
    return 0


def _group_by_folder(changes: List[CompiledChange]) -> Dict[str, List[CompiledChange]]:
    out: Dict[str, List[CompiledChange]] = {}
    for ch in changes:
        out.setdefault(ch.rule.folder, []).append(ch)
    return out


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fwgitops", description="GitOps firewall compiler")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("compile", help="compile intent → rules.auto.tfvars.json")
    c.add_argument("intent_root", nargs="?", default="intent", type=Path,
                   help="directory of intent YAML (default: intent)")
    c.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path,
                   help="environment resolution map (default: catalog/environments.yaml)")
    c.add_argument("--out", default=Path("terraform"), type=Path,
                   help="output root; writes <out>/<folder>/rules.auto.tfvars.json")
    c.add_argument("--check", action="store_true",
                   help="validate and report without writing files")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compile":
        return run_compile(
            args.intent_root, args.env_map, args.out, write=not args.check
        )
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
