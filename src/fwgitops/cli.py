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
import json
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


def run_push(
    folder: str,
    *,
    admins: Optional[List[str]] = None,
    all_admins: bool = False,
    session=None,
    out=None,
    err=None,
) -> int:
    """Push a folder's staged config to SCM (T13). Returns a process exit code.

    Exit codes:  0 ok/noop · 1 config/auth · 3 push failed.
    Credentials come from SCM_* env; the scm session does its own OAuth. The push
    is ADMIN-SCOPED — it commits only `admins`' staged changes (default: the
    service-account identity), so a shared-candidate folder with out-of-band edits
    is safe by construction. `--all-admins` is the break-glass (whole candidate).
    `session` is injectable for testing.
    """
    # Imported lazily so `fwgitops compile` never needs the SCM stack.
    from fwgitops.clients import ScmPushClient
    from fwgitops.push import PushError, push_folder
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1

    scope = admins or [session.credentials.client_id]
    client = ScmPushClient(session)
    try:
        result = push_folder(client, folder, admins=scope, all_admins=all_admins)
    except (PushError, ScmApiError) as e:
        print(f"PUSH FAILED: {e}", file=err)
        return 3

    print(f"OK — {result.status} (folder={result.folder} job={result.job_id})", file=out)
    print(json.dumps(result.to_evidence(), sort_keys=True), file=out)
    return 0


def run_onboard(
    serial: str,
    *,
    folder: str,
    name: Optional[str] = None,
    session=None,
    out=None,
    err=None,
) -> int:
    """Finalize device onboarding: verify placement, then set the display name.

    Exit codes:  0 ok · 1 config/auth · 3 onboard failed. The serial is captured
    upstream (ssh 'show system info'); this runs as the policy SA (folders read +
    device PUT are permitted). `session` is injectable for testing.
    """
    from fwgitops.clients import ScmDeviceClient
    from fwgitops.onboard import OnboardError, onboard_device
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1
    try:
        result = onboard_device(ScmDeviceClient(session), serial, expected_folder=folder, display_name=name)
    except (OnboardError, ScmApiError) as e:
        print(f"ONBOARD FAILED: {e}", file=err)
        return 3

    named = f" as {result.display_name!r}" if result.display_name else ""
    print(f"OK — {serial} in folder {result.folder!r}{named}", file=out)
    print(json.dumps(result.to_evidence(), sort_keys=True), file=out)
    return 0


def run_deregister(serial: str, *, session=None, out=None, err=None) -> int:
    """Remove a device's SCM registration (teardown; destroy does NOT do this).

    Exit codes:  0 ok · 1 config/auth · 3 deregister failed.
    """
    from fwgitops.clients import ScmDeviceClient
    from fwgitops.onboard import deregister_device
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1
    try:
        deregister_device(ScmDeviceClient(session), serial)
    except ScmApiError as e:
        print(f"DEREGISTER FAILED: {e}", file=err)
        return 3
    print(f"OK — deregistered {serial}", file=out)
    return 0


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

    p = sub.add_parser("push", help="push a folder's staged config to SCM (T13)")
    p.add_argument("folder", help="SCM folder to push")
    p.add_argument("--admin", action="append", dest="admins",
                   help="identity whose staged changes to commit (repeatable); "
                        "default: SCM_CLIENT_ID. Scopes the commit so out-of-band "
                        "edits are never swept in.")
    p.add_argument("--all-admins", action="store_true",
                   help="BREAK-GLASS: push the WHOLE candidate (every editor's staged "
                        "changes), e.g. to absorb the device-onboarding baseline")

    o = sub.add_parser("onboard", help="finalize onboarding: verify placement + set display name")
    o.add_argument("serial", help="device serial number (from ssh 'show system info')")
    o.add_argument("--folder", required=True,
                   help="SCM folder the device should have auto-placed into")
    o.add_argument("--name", help="display name to set in SCM (e.g. fw-prod-edge-682)")

    d = sub.add_parser("deregister", help="remove a device's SCM registration (teardown)")
    d.add_argument("serial", help="device serial number")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compile":
        return run_compile(
            args.intent_root, args.env_map, args.out, write=not args.check
        )
    if args.command == "push":
        return run_push(
            args.folder,
            admins=args.admins,
            all_admins=args.all_admins,
        )
    if args.command == "onboard":
        return run_onboard(args.serial, folder=args.folder, name=args.name)
    if args.command == "deregister":
        return run_deregister(args.serial)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
