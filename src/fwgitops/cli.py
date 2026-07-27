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
import os
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
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
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
    catalog, ok = _load_service_catalog(service_catalog_path, err)
    if not ok:
        return 1
    app_catalog, ok = _load_app_catalog(app_catalog_path, err)
    if not ok:
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
            ar = load_intent(doc, service_catalog=catalog, app_catalog=app_catalog)
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


def _load_service_catalog(path: Path, err) -> Tuple[Optional[object], bool]:
    """Load the optional service catalog. Returns (catalog_or_None, ok).

    Absent is fine — the explicit protocol+port form still works (Phase 1). A
    present-but-malformed catalog is a hard error (fail-closed).
    """
    if not path.is_file():
        return None, True
    from fwgitops.catalog import CatalogError, ServiceCatalog
    try:
        return ServiceCatalog.from_dict(read_yaml(path)), True
    except CatalogError as e:
        print(f"error: invalid service catalog {path}: {e}", file=err)
        return None, False
    except Exception as e:  # noqa: BLE001 - YAML parse / IO
        print(f"error: could not read service catalog {path}: {e}", file=err)
        return None, False


def _load_app_catalog(path: Path, err) -> Tuple[Optional[object], bool]:
    """Load the optional app catalog. Returns (catalog_or_None, ok).

    Absent is fine — explicit cidr/fqdn endpoints still work. Present-but-malformed
    is a hard error (fail-closed).
    """
    if not path.is_file():
        return None, True
    from fwgitops.catalog import AppCatalog, CatalogError
    try:
        return AppCatalog.from_dict(read_yaml(path)), True
    except CatalogError as e:
        print(f"error: invalid app catalog {path}: {e}", file=err)
        return None, False
    except Exception as e:  # noqa: BLE001 - YAML parse / IO
        print(f"error: could not read app catalog {path}: {e}", file=err)
        return None, False


def run_classify(
    intent_root: Path,
    env_map_path: Path,
    *,
    gate: Optional[str] = None,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    out=None,
    err=None,
) -> int:
    """Risk-classify every intent (Phase 2). Returns a process exit code.

    Compiles each intent (same fail-closed path as `compile`) then runs the
    policy-as-code classifier, reporting the risk tier + fired checks per change.

    `gate` is the max tier that may auto-apply. If any change exceeds it, the
    command FAILS (exit 3) — the fail-closed tier gate the apply pipeline uses so
    HIGH/CRITICAL changes need an explicit human override, not silent auto-apply.
    Exit codes:  0 ok · 1 usage/IO error · 2 invalid intent · 3 gate exceeded.
    """
    from fwgitops.classify import TIERS, PolicyContext, classify

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
    catalog, ok = _load_service_catalog(service_catalog_path, err)
    if not ok:
        return 1
    app_catalog, ok = _load_app_catalog(app_catalog_path, err)
    if not ok:
        return 1
    if not intent_root.exists():
        print(f"error: intent root not found: {intent_root}", file=err)
        return 1

    intents = discover_intents(intent_root)
    if not intents:
        print(f"no intent files found under {intent_root}", file=out)
        return 0

    changes: List[CompiledChange] = []
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            changes.append(compile_request(
                load_intent(doc, service_catalog=catalog, app_catalog=app_catalog), env_map))
        except IntentError as e:
            problems.append(f"{rel}:\n" + "\n".join(f"    {p}" for p in e.problems))
        except ResolveError as e:
            problems.append(f"{rel}: {e}")

    if problems:
        print(f"REJECTED — {len(problems)} of {len(intents)} intent file(s) invalid:", file=err)
        for p in problems:
            print(f"  - {p}", file=err)
        return 2

    # The rest of the declared policy — each change is classified against it
    # (GitOps = source of truth), enabling stateful checks (novel zone-pair, etc.).
    policy = PolicyContext.from_changes(changes)
    tiers = {"LOW": 0, "HIGH": 0, "CRITICAL": 0}
    exceeded: List[str] = []
    gate_rank = TIERS.index(gate) if gate else None
    for ch in sorted(changes, key=lambda c: c.rule.name):
        v = classify(ch, policy=policy)
        tiers[v.tier] = tiers.get(v.tier, 0) + 1
        checks = ", ".join(f["check"] for f in v.checks_fired) or "-"
        print(f"  {ch.rule.name:16} {v.tier:9} {checks}", file=out)
        if gate_rank is not None and TIERS.index(v.tier) > gate_rank:
            exceeded.append(f"{ch.rule.name}={v.tier}")
    print(
        f"classified {len(changes)}: "
        f"{tiers['LOW']} LOW · {tiers['HIGH']} HIGH · {tiers['CRITICAL']} CRITICAL",
        file=out,
    )
    if exceeded:
        print(
            f"::error::GATE — {len(exceeded)} change(s) exceed max-auto-tier {gate}: "
            f"{', '.join(exceeded)}. Re-dispatch with a higher override to apply these.",
            file=err,
        )
        return 3
    return 0


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


def run_set_admin_password(
    mgmt_ip: str,
    *,
    ssh_key: str,
    phash: Optional[str] = None,
    user: str = "admin",
    out=None,
    err=None,
) -> int:
    """Set the firewall admin password post-boot over SSH (route B).

    The phash ($5$… crypt hash) comes from FWGITOPS_ADMIN_PHASH (never a CLI arg,
    so it stays out of argv/process listings). Exit codes: 0 ok · 1 config · 3 failed.
    """
    from fwgitops.admin_password import AdminPasswordError, set_admin_phash

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    phash = phash or os.environ.get("FWGITOPS_ADMIN_PHASH")
    if not phash:
        print("error: set FWGITOPS_ADMIN_PHASH to the $5$… hash "
              "(openssl passwd -5 '<pw>' on Linux, or PAN-OS 'request password-hash')", file=err)
        return 1
    try:
        set_admin_phash(mgmt_ip, phash, ssh_key=ssh_key, user=user)
    except AdminPasswordError as e:
        print(f"SET-PASSWORD FAILED: {e}", file=err)
        return 3
    print(f"OK — admin password set on {mgmt_ip}", file=out)
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
    c.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path,
                   help="service name catalog (Phase 2); absent = explicit protocol+port only")
    c.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path,
                   help="app name catalog (Phase 2); absent = explicit cidr/fqdn only")

    cl = sub.add_parser("classify", help="risk-classify intents (Phase 2, policy-as-code)")
    cl.add_argument("intent_root", nargs="?", default="intent", type=Path,
                    help="directory of intent YAML (default: intent)")
    cl.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path,
                    help="environment resolution map (default: catalog/environments.yaml)")
    cl.add_argument("--gate", choices=("LOW", "HIGH", "CRITICAL"),
                    help="fail (exit 3) if any change's tier exceeds this max-auto tier")
    cl.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path,
                    help="service name catalog (Phase 2)")
    cl.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path,
                    help="app name catalog (Phase 2)")

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

    a = sub.add_parser("set-admin-password",
                       help="set the firewall admin password post-boot over SSH (route B)")
    a.add_argument("mgmt_ip", help="firewall management IP")
    a.add_argument("--ssh-key", required=True, help="path to the SSH private key (EC2 key pair)")
    a.add_argument("--user", default="admin", help="admin username (default: admin)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compile":
        return run_compile(
            args.intent_root, args.env_map, args.out, write=not args.check,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "classify":
        return run_classify(
            args.intent_root, args.env_map, gate=args.gate,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
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
    if args.command == "set-admin-password":
        return run_set_admin_password(args.mgmt_ip, ssh_key=args.ssh_key, user=args.user)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
