"""fwgitops command-line interface.

    fwgitops compile [intent_root] --env-map catalog/environments.yaml --out terraform

Reads intent YAML, validates + compiles each request, and writes per SCM folder
`rules.auto.tfvars.json` (AccessRequest) and `zones.auto.tfvars.json`
(ZoneRequest) for the static Terraform module. This is the entry point the
GitHub Actions pipeline calls on a PR.

Fail-closed and all-or-nothing: if ANY intent is invalid or unresolvable, the
aggregated problem report goes to stderr and nothing is written (exit 2). A run
either produces a clean, complete desired-state for every touched folder, or it
produces nothing. The same applies to the compiler → Terraform contract: emitting
data that no Terraform module declares AND wires is a rejection, not a silent
no-op (see `tfcontract.py` and ADR-0004).

Exit codes:  0 ok · 2 validation/compile/contract error · 1 usage/IO error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fwgitops.compiler import (
    CompileError,
    CompiledChange,
    CompiledZone,
    check_zone_collisions,
    check_zone_consistency,
    compile_any,
    dumps_payload,
    to_tfvars,
    zone_tfvars,
)
from fwgitops.intent import IntentError, load_intent
from fwgitops.io import discover_intents, read_yaml
from fwgitops.resolve import EnvMap, ResolveError
from fwgitops.tfcontract import check_contract, check_object_attributes, is_terraform_root

OUTPUT_FILENAME = "rules.auto.tfvars.json"
ZONES_FILENAME = "zones.auto.tfvars.json"


def run_compile(
    intent_root: Path,
    env_map_path: Path,
    out_root: Path,
    *,
    write: bool = True,
    require_terraform_root: bool = True,
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
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1

    if not intent_root.exists():
        print(f"error: intent root not found: {intent_root}", file=err)
        return 1

    intents = discover_intents(intent_root)
    if not intents:
        print(f"no intent files found under {intent_root} (nothing to compile)", file=out)
        return 0

    compiled: List[object] = []  # mixed CompiledChange | CompiledZone (per kind)
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001 - report any parse failure per file
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            req = load_intent(doc, **cats)
            compiled.append(compile_any(req, env_map))
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

    changes = [c for c in compiled if isinstance(c, CompiledChange)]
    zones = [c for c in compiled if isinstance(c, CompiledZone)]

    # Cross-kind check (ADR-0001): a rule may only use a zone declared by the env
    # map or a ZoneRequest — caught here, not at the firewall's device commit.
    # The collision check runs alongside: consistency UNIONS baseline + declared,
    # so it cannot see a ZoneRequest that would clobber an existing device zone.
    zone_violations = check_zone_consistency(changes, zones, env_map)
    zone_violations += check_zone_collisions(zones, env_map)
    if zone_violations:
        # "zone problem(s)", not "zone-consistency": a collision is the OPPOSITE
        # of a consistency failure (the zone is maximally consistent — it already
        # exists), so the old label mis-attributed it.
        print(f"REJECTED — {len(zone_violations)} zone problem(s); nothing written:",
              file=err)
        for v in zone_violations:
            print(f"  - {v}", file=err)
        return 2

    # Plan every file BEFORE writing any, so the contract check below can reject
    # the whole compile without leaving a half-written terraform/ directory.
    zones_by_folder: Dict[str, List[CompiledZone]] = {}
    for z in zones:
        zones_by_folder.setdefault(z.folder, []).append(z)

    # Build each payload ONCE. `to_tfvars` / `zone_tfvars` are not pure lookups —
    # they raise on duplicate keys — so calling them twice per folder would run
    # that check (and any future side effect) twice.
    planned: List[Tuple[Path, str, Dict[str, Any]]] = []  # (target, json, payload)
    try:
        for folder, folder_changes in sorted(_group_by_folder(changes).items()):
            payload = to_tfvars(folder_changes)
            target = out_root / folder / OUTPUT_FILENAME
            planned.append((target, dumps_payload(payload), payload))
        # ZoneRequest -> zones.auto.tfvars.json per folder (terraform auto-loads both).
        for folder, folder_zones in sorted(zones_by_folder.items()):
            payload = zone_tfvars(folder_zones)
            target = out_root / folder / ZONES_FILENAME
            planned.append((target, dumps_payload(payload), payload))
    except CompileError as e:
        # Duplicate zone key / object-name collision. These already fail closed
        # (nothing is written), but escaped as a raw traceback and exit 1 —
        # every other compile-stage rejection reports and returns 2.
        print(f"REJECTED — {e}; nothing written:", file=err)
        return 2

    # FAIL-CLOSED: refuse to emit data no Terraform module consumes.
    #
    # Terraform ignores an auto-tfvars key with no matching variable (warning,
    # exit 0), and ignores a declared-but-unwired variable with no message at
    # all. Either way the config never reaches the firewall while every check
    # stays green — that is exactly how ZoneRequest shipped as a dead end.
    # A MISSING Terraform root is itself a violation by default. Gating the
    # check on "does a Terraform root exist" made the check that catches missing
    # Terraform skip exactly when Terraform was missing: add an environment
    # whose terraform/<folder>/ does not exist yet and compile passed, then both
    # CI loops skipped the folder (`[ -f "$dir/main.tf" ] || continue`), so the
    # plan and its undeclared-variable grep never ran either. Green all the way
    # down, config never reaching the device. Callers that genuinely target a
    # scratch directory opt out explicitly via require_terraform_root=False.
    contract_problems: List[str] = []
    for target, _json, payload in planned:
        module_dir = target.parent
        if require_terraform_root or is_terraform_root(module_dir):
            contract_problems.extend(check_contract(module_dir, sorted(payload)))
        if is_terraform_root(module_dir):
            # HOLE 3: the key can be declared and wired while the object TYPE
            # omits attributes the compiler emits. Terraform discards those
            # silently, so the module falls back to its own defaults.
            for key, value in sorted(payload.items()):
                contract_problems.extend(check_object_attributes(module_dir, key, value))
    if contract_problems:
        print(
            f"REJECTED — {len(contract_problems)} Terraform contract problem(s); "
            f"nothing written:",
            file=err,
        )
        for p in contract_problems:
            print(f"  - {p}", file=err)
        return 2

    written: List[Path] = []
    for target, payload_json, _payload in planned:
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload_json, encoding="utf-8")
        written.append(target)

    verb = "wrote" if write else "would write"
    print(
        f"OK — compiled {len(compiled)} request(s) into {len(written)} file(s); {verb}:",
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


def _load_name_catalog(
    path: Path, *, kind: str, key: str, err, always_valid=frozenset()
) -> Tuple[Optional[object], bool]:
    """Load an optional NameCatalog (profiles/App-ID/log-forwarding). (cat_or_None, ok).

    Absent is fine — the field is then accepted free-form (back-compat). A
    present-but-malformed catalog is a hard error (fail-closed).
    """
    if not path.is_file():
        return None, True
    from fwgitops.catalog import CatalogError, NameCatalog
    try:
        return NameCatalog.from_dict(read_yaml(path), kind=kind, key=key,
                                     always_valid=always_valid), True
    except CatalogError as e:
        print(f"error: invalid {kind} catalog {path}: {e}", file=err)
        return None, False
    except Exception as e:  # noqa: BLE001 - YAML parse / IO
        print(f"error: could not read {kind} catalog {path}: {e}", file=err)
        return None, False


def _load_catalogs(
    service_catalog_path: Path, app_catalog_path: Path, err
) -> Tuple[Optional[Dict[str, object]], bool]:
    """Load every reference catalog into a kwargs dict for `load_intent`.

    The name catalogs live at conventional paths next to the service catalog
    (`profiles.yaml`, `applications.yaml`, `log-forwarding.yaml`, and
    `zone-protection.yaml` for ZoneRequest); each is optional. Returns
    (kwargs, ok) — any malformed catalog fails the run.
    """
    catalog_dir = service_catalog_path.parent
    svc, ok = _load_service_catalog(service_catalog_path, err)
    if not ok:
        return None, False
    app, ok = _load_app_catalog(app_catalog_path, err)
    if not ok:
        return None, False
    prof, ok = _load_name_catalog(catalog_dir / "profiles.yaml",
                                  kind="security profile group", key="profiles", err=err)
    if not ok:
        return None, False
    appid, ok = _load_name_catalog(catalog_dir / "applications.yaml",
                                   kind="App-ID", key="applications",
                                   always_valid=frozenset({"any"}), err=err)
    if not ok:
        return None, False
    logf, ok = _load_name_catalog(catalog_dir / "log-forwarding.yaml",
                                  kind="log-forwarding profile", key="profiles", err=err)
    if not ok:
        return None, False
    # Zone PROTECTION profiles — flood/recon, bound to a zone. Distinct from
    # profiles.yaml, which lists security profile GROUPS bound to a rule.
    zoneprot, ok = _load_name_catalog(catalog_dir / "zone-protection.yaml",
                                      kind="zone-protection profile", key="profiles", err=err)
    if not ok:
        return None, False
    return {
        "service_catalog": svc, "app_catalog": app, "profile_catalog": prof,
        "application_catalog": appid, "log_forwarding_catalog": logf,
        "zone_protection_catalog": zoneprot,
    }, True


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
    from fwgitops.classify import TIERS, PolicyContext, classify, classify_zone

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
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
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
    zones: List[CompiledZone] = []
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            c = compile_any(load_intent(doc, **cats), env_map)
            if isinstance(c, CompiledZone):
                zones.append(c)
            if isinstance(c, CompiledChange):
                changes.append(c)
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

    # Zones are classified too (ADR-0001 kind #2). A zone with no protection
    # profile has no flood/recon protection, and User-ID off silently breaks any
    # rule matching on source_user — both are findings, not defaults.
    for z in sorted(zones, key=lambda z: (z.folder, z.name)):
        v = classify_zone(z)
        tiers[v.tier] = tiers.get(v.tier, 0) + 1
        checks = ", ".join(f["check"] for f in v.checks_fired) or "-"
        print(f"  zone/{z.name:11} {v.tier:9} {checks}", file=out)
        if gate_rank is not None and TIERS.index(v.tier) > gate_rank:
            exceeded.append(f"zone/{z.name}={v.tier}")

    for ch in sorted(changes, key=lambda c: c.rule.name):
        v = classify(ch, policy=policy)
        tiers[v.tier] = tiers.get(v.tier, 0) + 1
        checks = ", ".join(f["check"] for f in v.checks_fired) or "-"
        print(f"  {ch.rule.name:16} {v.tier:9} {checks}", file=out)
        if gate_rank is not None and TIERS.index(v.tier) > gate_rank:
            exceeded.append(f"{ch.rule.name}={v.tier}")
    print(
        f"classified {len(changes) + len(zones)}: "
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


def _compile_intents(intent_root, env_map_path, cats, err):
    """Shared load+compile for classify/evidence/drift. Returns (items, code).

    items = list of (path, request, change) on success (code 0); None on error
    (code 1 usage/IO, 2 invalid intent) — the caller returns that code. `cats` is
    the load_intent kwargs dict from `_load_catalogs`.
    """
    if not env_map_path.is_file():
        print(f"error: env map not found: {env_map_path}", file=err)
        return None, 1
    try:
        env_map = EnvMap.from_dict(read_yaml(env_map_path))
    except ResolveError as e:
        print(f"error: invalid env map {env_map_path}: {e}", file=err)
        return None, 1
    if not intent_root.exists():
        print(f"error: intent root not found: {intent_root}", file=err)
        return None, 1
    intents = discover_intents(intent_root)
    items, problems = [], []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            ar = load_intent(doc, **cats)
            ch = compile_any(ar, env_map)
            if isinstance(ch, CompiledChange):  # rules only (zones are infra)
                items.append((path, ar, ch))
        except IntentError as e:
            problems.append(f"{rel}:\n" + "\n".join(f"    {p}" for p in e.problems))
        except ResolveError as e:
            problems.append(f"{rel}: {e}")
    if problems:
        print(f"REJECTED — {len(problems)} of {len(intents)} intent file(s) invalid:", file=err)
        for p in problems:
            print(f"  - {p}", file=err)
        return None, 2
    return items, 0


def run_drift(
    intent_root: Path,
    env_map_path: Path,
    snapshot_path: Path,
    *,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    out=None,
    err=None,
) -> int:
    """Detect drift: declared intents vs a snapshot of SCM's actual rules.

    The snapshot is a JSON/YAML list of {folder, name, tags} (what a folder rule
    read returns). Reports UNMANAGED (added outside GitOps), ORPHANED (managed but
    no longer declared), and MALFORMED rules. Exit: 0 clean · 1 usage · 2 invalid
    intent · 3 drift found.
    """
    from fwgitops.drift import ActualRule, detect_drift

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code

    if not snapshot_path.is_file():
        print(f"error: SCM snapshot not found: {snapshot_path}", file=err)
        return 1
    try:
        raw = read_yaml(snapshot_path)
    except Exception as e:  # noqa: BLE001
        print(f"error: could not read snapshot {snapshot_path}: {e}", file=err)
        return 1
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print(f"error: snapshot must be a list of {{folder, name, tags}} rules", file=err)
        return 1
    actual = []
    for i, x in enumerate(rows):
        if not isinstance(x, dict) or "folder" not in x or "name" not in x:
            print(f"error: snapshot[{i}] must have 'folder' and 'name'", file=err)
            return 1
        actual.append(ActualRule(folder=str(x["folder"]), name=str(x["name"]),
                                  tags=tuple(x.get("tags", []) or [])))

    report = detect_drift([ch for _, _, ch in items], actual)
    print(report.summary(), file=out)
    return 0 if report.is_clean else 3


def run_evidence(
    intent_root: Path,
    env_map_path: Path,
    out_root: Path,
    *,
    status: str = "applied",
    tfvars_root: Path = Path("terraform"),
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    out=None,
    err=None,
) -> int:
    """Write a NIST-mapped evidence bundle per change (Phase 2).

    Compiles + classifies every intent, then assembles one bundle per change with
    the risk verdict, intent/tfvars hashes, and CI provenance (from GITHUB_* env),
    written to `out_root/<folder>/<req_id>.json`. This is the apply-path audit
    record — run it after apply/push and upload the folder as a run artifact.
    Exit codes:  0 ok · 1 usage/IO/build error · 2 invalid intent.
    """
    import os
    from datetime import datetime, timezone

    from fwgitops.classify import PolicyContext, classify
    from fwgitops.evidence import CIContext, EvidenceError, build_bundle, sha256_file, write_bundle

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
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    if not intent_root.exists():
        print(f"error: intent root not found: {intent_root}", file=err)
        return 1

    intents = discover_intents(intent_root)
    if not intents:
        print(f"no intent files found under {intent_root}", file=out)
        return 0

    items = []  # (path, request, change)
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            ar = load_intent(doc, **cats)
            ch = compile_any(ar, env_map)
            if isinstance(ch, CompiledChange):  # evidence covers policy; zones are infra (follow-up)
                items.append((path, ar, ch))
        except IntentError as e:
            problems.append(f"{rel}:\n" + "\n".join(f"    {p}" for p in e.problems))
        except ResolveError as e:
            problems.append(f"{rel}: {e}")
    if problems:
        print(f"REJECTED — {len(problems)} of {len(intents)} intent file(s) invalid:", file=err)
        for p in problems:
            print(f"  - {p}", file=err)
        return 2

    policy = PolicyContext.from_changes([ch for _, _, ch in items])
    ci = CIContext.from_env(os.environ)
    now = datetime.now(timezone.utc)
    written: List[Path] = []
    for path, ar, ch in items:
        tfvars = tfvars_root / ch.rule.folder / "rules.auto.tfvars.json"
        try:
            bundle = build_bundle(
                request=ar, change=ch, status=status, generated_at=now,
                intent_sha256=sha256_file(path), intent_path=_display_path(path),
                tfvars_sha256=sha256_file(tfvars) if tfvars.is_file() else None,
                risk=classify(ch, policy=policy), ci=ci,
            )
        except EvidenceError as e:
            print(f"error: could not build evidence for {ch.rule.name}: {e}", file=err)
            return 1
        written.append(write_bundle(bundle, out_root))

    print(f"wrote {len(written)} evidence bundle(s) to {out_root}:", file=out)
    for p in written:
        print(f"  - {p}", file=out)
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


def run_enrich(
    folder: str,
    intent_root: Path,
    env_map_path: Path,
    *,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    dry_run: bool = False,
    session=None,
    out=None,
    err=None,
) -> int:
    """Write the security-rule fields the scm provider drops (ADR-0003 enrich).

    Runs AFTER `terraform apply` and BEFORE `fwgitops push`: it compiles the
    intents (same fail-closed path as compile/classify), filters to `folder`, and
    PUTs application/profile_setting/log_setting + ordering onto each managed rule
    via the SCM API — landing in the same candidate so the push commits skeleton +
    enrichment atomically. Exit codes: 0 ok/noop · 1 config/auth · 2 invalid intent
    · 3 enrich failed.

    `dry_run` previews what WOULD be set (from the compiled intents) and makes NO
    SCM calls — safe for PR CI, where the terraform plan alone no longer shows
    these fields (the module is skeleton-only; enrich owns them).
    """
    from fwgitops.clients import ScmRuleClient
    from fwgitops.enrich import EnrichError, _position_str, enrich_folder
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code
    changes = [ch for _, _, ch in items
               if isinstance(ch, CompiledChange) and ch.rule.folder == folder]
    if not changes:
        print(f"no managed rules for folder {folder!r} — nothing to enrich", file=out)
        return 0

    if dry_run:
        # PR preview: what enrich WOULD set on each rule. No SCM contact.
        print(f"enrich (dry-run) — folder {folder!r}: {len(changes)} rule(s):", file=out)
        for ch in sorted(changes, key=lambda c: c.rule.name):
            r = ch.rule
            neg = []
            if r.negate_source:
                neg.append("src")
            if r.negate_destination:
                neg.append("dst")
            extras = f" negate={','.join(neg)}" if neg else ""
            if r.source_user != ["any"]:
                extras += f" user={list(r.source_user)}"
            if r.category != ["any"]:
                extras += f" url-category={list(r.category)}"
            if r.log_start:
                extras += " log_start=true"
            if r.description:
                extras += f" description={r.description!r}"
            print(f"  {r.name}: action={r.action} application={list(r.application)} "
                  f"profile={r.profile_group or '(none)'} "
                  f"log_forwarding={r.log_setting or '(none)'} "
                  f"position={_position_str(r)}{extras}", file=out)
        return 0

    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1
    try:
        result = enrich_folder(ScmRuleClient(session), folder, changes)
    except (EnrichError, ScmApiError) as e:
        print(f"ENRICH FAILED: {e}", file=err)
        return 3

    print(f"OK — enriched {len(result.records)} rule(s) in folder {folder!r}", file=out)
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


def run_rules(
    folder: str,
    *,
    contains: Optional[str] = None,
    session=None,
    out=None,
    err=None,
) -> int:
    """List the security rules currently LIVE in an SCM folder (reads SCM, no UI).

    This is the "is my rule deployed?" check — it queries SCM's committed state,
    so it is reliable any time (no apply run needed). With `--has <id>` it checks
    one rule and exits 3 if absent (scriptable). Credentials come from SCM_* env.
    Exit codes: 0 ok/present · 1 config/auth · 3 rule not found.
    """
    from fwgitops.clients import ScmRuleClient
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
        names = sorted(ScmRuleClient(session).rule_ids_by_name(folder))
    except ScmApiError as e:
        print(f"error: could not read folder {folder!r}: {e}", file=err)
        return 1

    if contains is not None:
        present = contains in names
        print(f"{contains}: {'LIVE' if present else 'NOT FOUND'} in folder {folder!r}", file=out)
        return 0 if present else 3

    print(f"{len(names)} rule(s) live in folder {folder!r}:", file=out)
    for n in names:
        print(f"  {n}", file=out)
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
    c.add_argument("--allow-missing-root", action="store_true",
                   help="permit emitting into a folder with no Terraform root "
                        "(scratch/scaffold use only — normally a hard error)")
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

    e = sub.add_parser("evidence", help="write NIST-mapped evidence bundles per change (Phase 2)")
    e.add_argument("intent_root", nargs="?", default="intent", type=Path,
                   help="directory of intent YAML (default: intent)")
    e.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    e.add_argument("--out", default=Path("evidence"), type=Path,
                   help="output root; writes <out>/<folder>/<req_id>.json")
    e.add_argument("--status", default="applied", choices=("applied", "rejected", "failed"),
                   help="outcome recorded in each bundle (default: applied)")
    e.add_argument("--tfvars-root", default=Path("terraform"), type=Path,
                   help="where the compiled rules.auto.tfvars.json live (for the hash)")
    e.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    e.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)

    dr = sub.add_parser("drift", help="detect drift: declared intents vs an SCM rule snapshot (Phase 2)")
    dr.add_argument("intent_root", nargs="?", default="intent", type=Path)
    dr.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    dr.add_argument("--snapshot", required=True, type=Path,
                    help="JSON/YAML list of the folder's actual rules {folder, name, tags}")
    dr.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    dr.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)

    p = sub.add_parser("push", help="push a folder's staged config to SCM (T13)")
    p.add_argument("folder", help="SCM folder to push")
    p.add_argument("--admin", action="append", dest="admins",
                   help="identity whose staged changes to commit (repeatable); "
                        "default: SCM_CLIENT_ID. Scopes the commit so out-of-band "
                        "edits are never swept in.")
    p.add_argument("--all-admins", action="store_true",
                   help="BREAK-GLASS: push the WHOLE candidate (every editor's staged "
                        "changes), e.g. to absorb the device-onboarding baseline")

    en = sub.add_parser("enrich",
                        help="set the security-rule fields the scm provider drops (ADR-0003)")
    en.add_argument("folder", help="SCM folder to enrich (rules must already be applied)")
    en.add_argument("intent_root", nargs="?", default="intent", type=Path,
                    help="directory of intent YAML (default: intent)")
    en.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path,
                    help="environment resolution map (default: catalog/environments.yaml)")
    en.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    en.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)
    en.add_argument("--dry-run", action="store_true",
                    help="preview what would be set (no SCM calls) — for PR validation")

    o = sub.add_parser("onboard", help="finalize onboarding: verify placement + set display name")
    o.add_argument("serial", help="device serial number (from ssh 'show system info')")
    o.add_argument("--folder", required=True,
                   help="SCM folder the device should have auto-placed into")
    o.add_argument("--name", help="display name to set in SCM (e.g. fw-prod-edge-682)")

    rl = sub.add_parser("rules", help="list the security rules currently live in an SCM folder")
    rl.add_argument("folder", help="SCM folder to read")
    rl.add_argument("--has", dest="contains",
                    help="check a specific rule id is live (exit 3 if not found)")

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
            require_terraform_root=not args.allow_missing_root,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "classify":
        return run_classify(
            args.intent_root, args.env_map, gate=args.gate,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "evidence":
        return run_evidence(
            args.intent_root, args.env_map, args.out, status=args.status,
            tfvars_root=args.tfvars_root,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "drift":
        return run_drift(
            args.intent_root, args.env_map, args.snapshot,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "push":
        return run_push(
            args.folder,
            admins=args.admins,
            all_admins=args.all_admins,
        )
    if args.command == "enrich":
        return run_enrich(
            args.folder, args.intent_root, args.env_map,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
            dry_run=args.dry_run,
        )
    if args.command == "onboard":
        return run_onboard(args.serial, folder=args.folder, name=args.name)
    if args.command == "rules":
        return run_rules(args.folder, contains=args.contains)
    if args.command == "deregister":
        return run_deregister(args.serial)
    if args.command == "set-admin-password":
        return run_set_admin_password(args.mgmt_ip, ssh_key=args.ssh_key, user=args.user)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
