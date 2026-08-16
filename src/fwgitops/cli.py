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
from dataclasses import dataclass
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fwgitops.compiler import (
    CompileError,
    CompiledChange,
    CompiledZone,
    Scope,
    check_zone_collisions,
    check_zone_consistency,
    dumps_payload,
)
from fwgitops.intent import IntentError, load_intent
from fwgitops.kinds import (
    REGISTRY,
    KindOrderError,
    kind_apply_order,
    kinds_with_drift_engine,
    kinds_with_state_api,
    compile_any,
    group_by_kind_and_scope,
    handler_for_request,
    scopes_in_apply_order,
    of_kind,
)
from fwgitops.io import discover_intents, read_yaml
from fwgitops.removal import (
    classify_removal,
    parse_removes_trailers,
    removal_ticket_problems,
    stale_ticket_problems,
)
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
    record_violations: Optional[Path] = None,
    run_url: Optional[str] = None,
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
            req = load_intent(doc, env_map=env_map, **cats)
            mismatch = _id_matches_filename(path, req)
            if mismatch:
                problems.append(f"{rel}: {mismatch}")
                continue
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

    changes = of_kind(compiled, "AccessRequest")
    zones = of_kind(compiled, "ZoneRequest")

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
    #
    # ONE loop for every kind (ADR-0001). This used to be a hand-written block
    # per kind — the shape that let ZoneRequest ship with no Terraform behind it.
    # A new kind adds a REGISTRY entry and is emitted here automatically; if its
    # Terraform side is missing, the contract check below rejects the compile.
    #
    # Each payload is built ONCE: `tfvars` is not a pure lookup (it raises on
    # duplicate keys), so calling it twice per folder would run that check twice.
    planned: List[Tuple[Path, str, Dict[str, Any]]] = []  # (target, json, payload)
    try:
        # APPLY ORDER (ADR-0002), not alphabetical. Emission order does not
        # change what Terraform does inside one root, but the plan report is how
        # a reviewer sees the chain, and the apply pipeline consumes the same
        # sequence — so the two must not disagree. Scope key breaks ties, so the
        # output is stable between runs.
        groups = group_by_kind_and_scope(compiled)
        for kind, scope in scopes_in_apply_order(compiled):
            objs = groups[(kind, scope)]
            handler = REGISTRY[kind]
            payload = handler.tfvars(objs)
            # One root == one state (design Arch-2). A firewall's state must not
            # share a root with its folder's — a device write is a per-device
            # OVERRIDE of a different object, not an edit of the folder's.
            target = out_root / scope.dirname / handler.tfvars_filename
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

    # ── DELETE THE FILES THIS COMPILE NO LONGER PRODUCES ──────────────────
    # Removing the LAST intent of a kind from a folder must remove that kind's
    # tfvars file. Writing the files a compile produces is not enough: the
    # previous file stays on disk, Terraform auto-loads it, and the deleted
    # object is silently re-asserted.
    #
    # CI never saw this — `terraform/*/*.auto.tfvars.json` is gitignored, so a
    # clean checkout has no stale file and `var.<kind>` correctly falls back to
    # its `default = {}`. The damage is local: `terraform plan` reports
    # `No changes` for a deletion CI would perform. Found live 2026-08-05 while
    # testing the zone deletion path, where a real deletion looked like a no-op
    # on this machine while being correct in the pipeline. Verification that
    # lies is worse than none, because a confident wrong answer is
    # indistinguishable from a right one.
    #
    # ONLY files this tool owns are removed: the exact `tfvars_filename` of a
    # registered kind. An unknown `*.auto.tfvars.json` is left ALONE — someone
    # may hand-maintain one, and deleting a file we never wrote is not cleanup,
    # it is data loss.
    removed: List[Path] = []
    if write:
        owned = {h.tfvars_filename for h in REGISTRY.values() if h.tfvars_filename}
        produced = {t.resolve() for t in written}
        # Every scope directory under the out root, not just the ones this run
        # wrote to. Scoping the sweep to `written` would miss the sharpest case:
        # a folder losing its LAST intent of ANY kind writes nothing there, so
        # that directory would never be visited and every stale file in it would
        # survive — precisely the deletion this exists to make work.
        #
        # Safe because one compile processes the WHOLE intent tree, so
        # `produced` is complete for every scope. Directories holding no owned
        # file (modules/, bootstrap-*, github-oidc) are untouched by the name
        # check below, so there is no skip-list to keep in step.
        scope_dirs = {t.parent for t in written}
        if out_root.is_dir():
            scope_dirs |= {d for d in out_root.iterdir() if d.is_dir()}
        for scope_dir in sorted(scope_dirs):
            for stale in sorted(scope_dir.glob("*.auto.tfvars.json")):
                if stale.name in owned and stale.resolve() not in produced:
                    stale.unlink()
                    removed.append(stale)

    # WARN when objects land in a folder no firewall inherits from.
    #
    # That combination is the quietest failure this pipeline can produce:
    # compile succeeds, apply succeeds, the push succeeds trivially because there
    # is nothing to push to, and not one packet is filtered. Every signal is
    # green and the rule does not exist anywhere it matters.
    #
    # A WARNING, not a rejection. ADR-0002 has the folder created BEFORE the
    # firewall registers to it (the firewall names it as `dgname`), so a folder
    # legitimately has no devices during bring-up. Failing here would break the
    # documented Day-1 order; staying silent is how someone finds out from a
    # packet capture instead.
    hierarchy = cats.get("folder_hierarchy")
    if hierarchy is not None:
        with_devices = set(hierarchy.devices.values())
        empty = sorted({
            t.parent.name for t in written
            if not t.parent.name.startswith("device-")
            and t.parent.name not in with_devices
            and not hierarchy.children_of(t.parent.name)
        })
        for folder in empty:
            print(f"WARNING — folder {folder!r} has NO FIREWALL beneath it. The objects "
                  f"compiled here will apply and push successfully and enforce nothing, "
                  f"until a firewall registers to it.", file=err)

    verb = "wrote" if write else "would write"
    print(
        f"OK — compiled {len(compiled)} request(s) into {len(written)} file(s); {verb}:",
        file=out,
    )
    for t in written:
        print(f"  - {t}", file=out)
    for r in removed:
        print(f"  - {r} (REMOVED — no {r.name.split('.')[0]} remain in this scope)", file=out)
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


def _load_router_catalog(path: Path, err) -> Tuple[Optional[object], bool]:
    """Load the optional router/VRF topology catalog. Absent is fine until a
    RouteRequest needs it, at which point the loader reports the missing entry."""
    if not path.is_file():
        return None, True
    from fwgitops.catalog import CatalogError, RouterCatalog
    try:
        return RouterCatalog.from_dict(read_yaml(path)), True
    except (CatalogError, Exception) as e:  # noqa: BLE001
        print(f"error: invalid router catalog {path}: {e}", file=err)
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
    # Logical router / VRF topology (RouteRequest). Routes aggregate into one
    # router object that also holds interface membership, so membership must be
    # declared rather than inferred — see catalog/routers.yaml.
    routers, ok = _load_router_catalog(catalog_dir / "routers.yaml", err)
    if not ok:
        return None, False
    # Interface management profiles — which admin services answer on an
    # interface (InterfaceRequest).
    ifprof, ok = _load_name_catalog(catalog_dir / "interface-profiles.yaml",
                                    kind="interface management profile",
                                    key="profiles", err=err)
    if not ok:
        return None, False
    # Zone PROTECTION profiles — flood/recon, bound to a zone. Distinct from
    # profiles.yaml, which lists security profile GROUPS bound to a rule.
    zoneprot, ok = _load_name_catalog(catalog_dir / "zone-protection.yaml",
                                      kind="zone-protection profile", key="profiles", err=err)
    if not ok:
        return None, False
    # Folder hierarchy — validates a Day-1 kind's explicit `folder:` target.
    # Loaded here (not only in run_classify) because targetability is a LOAD-time
    # check: an intent naming a non-targetable folder must be rejected before it
    # compiles, not merely tiered up. Its absence makes `folder:` unusable rather
    # than unchecked — see `_load_target`.
    folders = None
    folders_path = catalog_dir / "folders.yaml"
    if folders_path.is_file():
        from fwgitops.catalog import FolderHierarchy
        try:
            folders = FolderHierarchy.from_dict(read_yaml(folders_path))
        except CatalogError as e:
            print(f"error: invalid folder hierarchy {folders_path}: {e}", file=err)
            return None, False
    # Interface ROLE -> object name per scope ($eth-local at folder scope,
    # ethernet1/4 at device scope — one object, two names, ADR-0005).
    ifaces = None
    ifaces_path = catalog_dir / "interfaces.yaml"
    if ifaces_path.is_file():
        from fwgitops.catalog import InterfaceCatalog
        try:
            ifaces = InterfaceCatalog.from_dict(read_yaml(ifaces_path))
        except CatalogError as e:
            print(f"error: invalid interface catalog {ifaces_path}: {e}", file=err)
            return None, False
    return {
        "interface_catalog": ifaces,
        "service_catalog": svc, "app_catalog": app, "profile_catalog": prof,
        "application_catalog": appid, "log_forwarding_catalog": logf,
        "zone_protection_catalog": zoneprot,
        "interface_profile_catalog": ifprof,
        "router_catalog": routers,
        "folder_hierarchy": folders,
    }, True


def run_where(
    query_text: str,
    intent_root: Path,
    env_map_path: Path,
    *,
    evidence_root: Path = Path("evidence"),
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    as_json: bool = False,
    out=None,
    err=None,
) -> int:
    """Map an address, name or ticket back to the intent that authorised it.

    The incident-response query: a log line gives an IP, and the question is which
    request permitted the traffic, who asked for it, and under what ticket.

    Matching is by CONTAINMENT, not text — the log says `10.20.9.10` and the
    intent says `10.20.9.0/24`, so grep answers "nothing", which at 3am is
    indistinguishable from "no rule permits this". See `fwgitops.where`.

    Exit codes:  0 hits found · 1 usage/IO error · 2 invalid intent · 4 no match.
    """
    import json as _json

    from fwgitops.where import Query, find

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code

    query = Query.parse(query_text)
    by_id = {}
    scope_by_id = {}
    searchable = []
    for path, req, ch in items:
        handler = handler_for_request(req)
        rid = req.metadata.id
        scope = handler.scope_of(ch)
        by_id[rid] = (path, req, handler)
        scope_by_id[rid] = scope
        searchable.append((handler.kind, rid, scope.key, ch))

    hits = find(query, searchable)

    # The METADATA query. A responder often holds a TICKET rather than an
    # address — "what did JIRA-12345 actually change?" — and metadata lives on
    # the request, not on anything compiled, so the generic walk cannot see it.
    if not query.is_address:
        from fwgitops.where import Hit
        for rid, (path, req, handler) in sorted(by_id.items()):
            for field, value in (("metadata.id", rid),
                                 ("metadata.ticket", req.metadata.ticket),
                                 ("metadata.requester", req.metadata.requester)):
                if str(value).lower() == query.text.lower():
                    hits.append(Hit(kind=handler.kind, req_id=rid,
                                    scope=scope_by_id[rid].key,
                                    field=field, value=str(value),
                                    why=f"{field} is exactly {query.text!r}"))

    records = []
    for h in hits:
        path, req, handler = by_id[h.req_id]
        # `dirname`, NOT `key`: a device scope keys as `device:<serial>` but its
        # evidence lands in `device-<serial>/`, mirroring the Terraform roots. A
        # colon in a path is the folder-vs-device confusion this project keeps
        # meeting, arriving through a report this time.
        ev = evidence_root / scope_by_id[h.req_id].dirname / f"{h.req_id}.json"
        records.append({
            "kind": h.kind, "req_id": h.req_id, "scope": h.scope,
            "matched": {"field": h.field, "value": h.value, "why": h.why},
            "effective_route": h.effective_route,
            "intent_file": _display_path(path),
            "ticket": req.metadata.ticket,
            "requester": req.metadata.requester,
            "requested": req.metadata.requested.isoformat(),
            "justification": req.metadata.justification,
            # Reported whether or not it exists. A missing bundle for a live
            # object is itself the finding — it means this change has no audit
            # record — and hiding the path would hide that.
            "evidence": _display_path(ev),
            "evidence_exists": ev.is_file(),
        })

    if as_json:
        print(_json.dumps(records, indent=2, sort_keys=True), file=out)
        return 0 if records else 4

    # SPLIT BY WHAT THE MATCH MEANS. A default route matches EVERY address, so a
    # flat "1 match" for an address no rule mentions reads as "something
    # authorised this" — the opposite of the truth, delivered to someone at 3am.
    # "What permits it" and "what carries it" are different questions and are
    # answered separately, so a silent rulebase stays visible.
    rules = [r for r in records if r["kind"] == "AccessRequest"]
    routes = [r for r in records if r["kind"] == "RouteRequest"]
    other = [r for r in records if r not in rules and r not in routes]

    def show(rs):
        for r in rs:
            star = "   <- CARRIES IT" if r["effective_route"] else ""
            print(f"  {r['kind']}  {r['req_id']}  ({r['scope']}){star}", file=out)
            print(f"      matched : {r['matched']['field']} = {r['matched']['value']}",
                  file=out)
            print(f"      why     : {r['matched']['why']}", file=out)
            print(f"      ticket  : {r['ticket']}  ({r['requester']}, "
                  f"{r['requested']})", file=out)
            print(f"      request : {r['justification']}", file=out)
            print(f"      intent  : {r['intent_file']}", file=out)
            ev = r["evidence"] if r["evidence_exists"] else \
                f"{r['evidence']}  (MISSING — this change has no audit record)"
            print(f"      evidence: {ev}\n", file=out)

    if not records:
        # EXPLICIT, and distinguished from an error. "Nothing here accounts for
        # it" is a real answer — it means the config came from somewhere else —
        # and it must not read like the command failed.
        print(f"no intent accounts for {query.text!r}.", file=out)
        print("  This is an ANSWER, not an error: nothing in this repository "
              "authorised it.", file=out)
        print("  Check `fwgitops drift` — config in SCM that GitOps did not put "
              "there looks exactly like this.", file=out)
        return 4

    print(f"{len(records)} match(es) for {query.text!r}\n", file=out)
    if query.is_address:
        print("RULES — what permits or denies it", file=out)
        if rules:
            show(rules)
        else:
            print(f"  NONE. No AccessRequest in this repository mentions "
                  f"{query.text}.\n"
                  f"  Traffic to or from it is decided by a rule declared "
                  f"elsewhere, by an\n"
                  f"  inherited rule, or by the folder's default — not by anything "
                  f"here.\n", file=out)
        if routes:
            print("ROUTES — what carries it", file=out)
            show(routes)
    else:
        show(rules + routes)
    if other:
        print("OTHER", file=out)
        show(other)
    return 0 if records else 4


def run_from_issue(
    body_path: Path,
    issue_number: int,
    author: str,
    *,
    out_root: Path = Path("."),
    write: bool = True,
    out=None,
    err=None,
) -> int:
    """Turn a filled Issue Form into an intent file (broad-requester intake).

    Rejections are written for a REQUESTER, naming the form field they filled in
    — they cannot act on `spec.service[0].protocol`, and a broken PR they cannot
    fix is worse than no intake at all.

    Prints the path it wrote (or would write) on stdout so CI can branch on it.
    Exit codes:  0 ok · 1 IO · 2 the form cannot be turned into a request.
    """
    import os

    from fwgitops.intake import IntakeError, build_intent, to_yaml

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        body = body_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: could not read {body_path}: {e}", file=err)
        return 1
    try:
        intake = build_intent(body, issue_number=issue_number, author=author)
    except IntakeError as e:
        print(f"REJECTED — {len(e.problems)} problem(s) with the request form:", file=err)
        for p in e.problems:
            print(f"  - {p}", file=err)
        return 2

    target = out_root / intake.path
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            to_yaml(intake, issue_number=issue_number, repo=os.environ.get("GITHUB_REPOSITORY")),
            encoding="utf-8")
    print(intake.path, file=out)
    return 0


def run_classify(
    intent_root: Path,
    env_map_path: Path,
    *,
    gate: Optional[str] = None,
    state_snapshot_paths: Optional[List[Path]] = None,
    baseline_root: Optional[Path] = None,
    baseline_catalog_dir: Optional[Path] = None,
    change_message_path: Optional[Path] = None,
    max_tier: bool = False,
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
    from fwgitops.classify import TIERS, PolicyContext

    # Folder hierarchy (optional). A change scoped to a folder WITH CHILDREN
    # reaches every one of them — the largest blast radius this platform can
    # produce, so the classifier tiers it up (ADR-0005). The classifier stays
    # pure: the hierarchy is declared config, not a live SCM read.
    # Current SCM state (optional). Without it the classifier cannot tell
    # "this zone gains its first interface" from "this zone's interfaces
    # change" — the same distinction ADR-0005 wants for interface addressing.
    # Absent snapshot disables those checks rather than guessing.
    current = None
    if state_snapshot_paths:
        current = {}
        for path in state_snapshot_paths:
            rows, code = _read_snapshot_rows(path, err)
            if rows is None:
                return code
            for x in rows:
                if isinstance(x, dict) and x.get("name"):
                    # Key on the QUERIED folder, not the one SCM reports the
                    # object as DEFINED in — an inherited object would otherwise
                    # never match its declaration and the check would silently
                    # never fire.
                    scope = str(x.get("scope") or x.get("folder"))
                    current[(scope, str(x["name"]))] = x

    hierarchy = None
    hierarchy_path = env_map_path.parent / "folders.yaml"
    if hierarchy_path.is_file():
        from fwgitops.catalog import CatalogError, FolderHierarchy
        try:
            hierarchy = FolderHierarchy.from_dict(read_yaml(hierarchy_path))
        except (CatalogError, Exception) as e:  # noqa: BLE001
            print(f"error: invalid folder hierarchy {hierarchy_path}: {e}", file=err)
            return 1

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
    # NO EARLY RETURN ON AN EMPTY TREE when a baseline is given. A PR that
    # deletes every intent leaves nothing to classify and is exactly when the
    # gate matters most — returning 0 here would wave through the largest
    # possible removal.
    if not intents and baseline_root is None:
        print(f"no intent files found under {intent_root}", file=out)
        return 0

    compiled: List[Any] = []
    #: id(compiled object) -> request id. Needed because ROUTING tiers the
    #: CHANGE, not the tree, and a compiled object does not always carry its
    #: request id (a zone is named `dmz`).
    req_id_of: Dict[int, str] = {}
    current_ids: Set[str] = set()
    problems: List[str] = []
    for path in intents:
        rel = _display_path(path)
        try:
            doc = read_yaml(path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{rel}: could not parse YAML: {e}")
            continue
        try:
            _ar = load_intent(doc, env_map=env_map, **cats)
            mismatch = _id_matches_filename(path, _ar)
            if mismatch:
                problems.append(f"{rel}: {mismatch}")
                continue
            c = compile_any(_ar, env_map)
            req_id_of[id(c)] = _ar.metadata.id
            current_ids.add(_ar.metadata.id)
            compiled.append(c)
        except IntentError as e:
            problems.append(f"{rel}:\n" + "\n".join(f"    {p}" for p in e.problems))
        except ResolveError as e:
            problems.append(f"{rel}: {e}")

    if problems:
        print(f"REJECTED — {len(problems)} of {len(intents)} intent file(s) invalid:", file=err)
        for p in problems:
            print(f"  - {p}", file=err)
        return 2

    # --max-tier wants ONE token on stdout. The per-change report still runs (the
    # tiers must be computed) but goes to a sink, so a caller can do
    # `tier=$(fwgitops classify intent --max-tier)` without parsing.
    import io as _io
    report = _io.StringIO() if max_tier else out

    # The rest of the declared policy — each change is classified against it
    # (GitOps = source of truth), enabling stateful checks (novel zone-pair, etc.).
    changes = of_kind(compiled, "AccessRequest")
    zones = of_kind(compiled, "ZoneRequest")
    policy = PolicyContext.from_changes(changes)
    tiers = {"LOW": 0, "HIGH": 0, "CRITICAL": 0}
    graded: List[Tuple[str, str]] = []      # (req_id, tier), for changeset routing
    changed_ids: Optional[Set[str]] = None  # None = no baseline, tier the whole tree
    exceeded: List[str] = []
    gate_rank = TIERS.index(gate) if gate else None

    # ONE loop over every registered kind (ADR-0001). This used to be a
    # hand-written block per kind — the shape that let ZoneRequest go
    # unclassified entirely ("policy stages: rules only"). Each handler takes
    # the context it needs and ignores the rest, so a new kind is classified
    # here the moment it is registered.
    for kind in sorted(REGISTRY):
        handler = REGISTRY[kind]
        for obj in sorted(of_kind(compiled, kind),
                          key=lambda o, h=handler: (h.scope_of(o).key, h.name_of(o))):
            v = handler.classify(obj, policy=policy, hierarchy=hierarchy, current=current)
            tiers[v.tier] = tiers.get(v.tier, 0) + 1
            graded.append((req_id_of.get(id(obj), ""), v.tier))
            checks = ", ".join(f["check"] for f in v.checks_fired) or "-"
            label = f"{handler.report_prefix}{handler.name_of(obj)}"
            print(f"  {label:22} {v.tier:9} {checks}", file=report)
            if gate_rank is not None and TIERS.index(v.tier) > gate_rank:
                exceeded.append(f"{label}={v.tier}")
    # ── REMOVALS ──────────────────────────────────────────────────────────
    # A deleted intent is ABSENT from the tree above, so nothing classified it
    # and the gate never saw it. Removing a rule that permits traffic and
    # removing a route that carries it were equally invisible. `baseline_root` is
    # the base revision's intent tree (CI materialises it with `git archive`);
    # comparing trees keeps this pure, unlike reading git here.
    removed_count = 0
    if baseline_root is not None:
        b_env, b_cats, b_ok = _baseline_catalogs(baseline_catalog_dir, env_map_path, err)
        if not b_ok:
            return 1
        removals, mods, code = _load_changeset(baseline_root, intent_root, env_map,
                                               cats, err,
                                               baseline_env_map=b_env,
                                               baseline_cats=b_cats)
        if removals is None:
            return code

        # A MODIFIED intent must carry its own change ticket. Without this the
        # evidence bundle for today's change names the request that authorised
        # the PREVIOUS one — a false statement in an artifact that claims NIST
        # CM-3. Rejected (exit 2) rather than tiered, because it is an invalid
        # intent, not a risky one.
        stale = stale_ticket_problems(mods)
        if stale:
            print(f"REJECTED — {len(stale)} modified intent(s) reuse the previous "
                  f"change ticket:", file=err)
            for p in stale:
                print(f"  - {p}", file=err)
            return 2
        # A REMOVAL must carry its own change ticket too, for the same reason a
        # modification must — and it cannot state it in the intent, because the
        # intent is what is being deleted. The `Removes:` trailer is where it
        # goes. Checked HERE, on the PR, while the author is still present to fix
        # it; the apply path checks again because that is what actually writes
        # the record.
        unauthorised = removal_ticket_problems(
            removals, _removes_trailers(change_message_path, err) or {})
        if unauthorised:
            print(f"REJECTED — {len(unauthorised)} removal(s) without an authorising "
                  f"ticket:", file=err)
            for p in unauthorised:
                print(f"  - {p}", file=err)
            return 2

        removed_count = len(removals)
        # THE CHANGESET: what this PR added, modified or removed. Routing tiers
        # THIS, not the tree — "how risky is this change?" is the question the
        # approver is being asked. Tiering the tree answers a different one, and
        # once a firewall has a default route the answer is permanently HIGH, so
        # nothing would ever auto-apply again.
        baseline_ids = {str(d.get("metadata", {}).get("id"))
                        for d in (read_yaml(p) for p in discover_intents(baseline_root))
                        if isinstance(d, dict)}
        changed_ids = ((current_ids - baseline_ids)          # added
                       | {m.req_id for m in mods}            # modified
                       | {r.req_id for r in removals})       # removed
        for r in removals:
            v = classify_removal(r)
            tiers[v.tier] = tiers.get(v.tier, 0) + 1
            graded.append((r.req_id, v.tier))
            checks = ", ".join(f["check"] for f in v.checks_fired) or "-"
            label = f"REMOVED {r.req_id}"
            # `report`, NOT `out` — the same stream the added/modified listing
            # above uses. Under `--max-tier` that is a buffer nobody reads, so
            # stdout carries the tier and nothing else. This line said `out`,
            # and the only changeset that reveals it is one containing a
            # REMOVAL: the workflow's `tier=$(fwgitops classify ...)` captured
            # two lines, and `echo "tier=$tier" >> $GITHUB_OUTPUT` died on
            # `Invalid format 'HIGH'`. Four lines below, the comment already
            # promised "nothing else on stdout".
            print(f"  {label:22} {v.tier:9} {checks}", file=report)
            if gate_rank is not None and TIERS.index(v.tier) > gate_rank:
                exceeded.append(f"{label}={v.tier}")

    # --max-tier: the HIGHEST tier in the changeset, and nothing else on stdout,
    # so CI can route on it. The apply workflow picks which environment (which
    # approver) a run needs from this, which is what makes "LOW auto-applies,
    # HIGH waits for a human" a property of the pipeline rather than a claim in
    # a README. Empty changeset -> LOW: nothing to apply cannot need an
    # approver, and the alternative (defaulting high) would gate every no-op.
    if max_tier:
        pool = [t for rid, t in graded
                if changed_ids is None or rid in changed_ids]
        # Empty changeset -> LOW. A no-op needs no approver, and defaulting high
        # would gate every run that changed nothing.
        highest = next((t for t in reversed(TIERS) if t in pool), "LOW")
        print(highest, file=out)
        return 0

    print(
        f"classified {len(compiled) + removed_count}: "
        f"{tiers['LOW']} LOW · {tiers['HIGH']} HIGH · {tiers['CRITICAL']} CRITICAL"
        + (f" ({removed_count} removal(s))" if removed_count else ""),
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


def _baseline_catalogs(baseline_catalog_dir: Optional[Path], env_map_path: Path, err):
    """(env_map, cats, ok) for a baseline tree, from the catalog that shipped with it.

    Returns (None, None, True) when no directory is given — the caller then
    reuses today's, which is the old behaviour and correct whenever the catalog
    has not moved.
    """
    if baseline_catalog_dir is None:
        return None, None, True
    d = Path(baseline_catalog_dir)
    if not d.is_dir():
        print(f"error: --baseline-catalog {d} is not a directory", file=err)
        return None, None, False
    cats, ok = _load_catalogs(d / "services.yaml", d / "apps.yaml", err)
    if not ok:
        return None, None, False
    # The env map lives beside the rest; fall back to today's if the archived
    # tree predates it rather than failing a run over a file that never moved.
    env_path = d / env_map_path.name
    if not env_path.is_file():
        return None, cats, True
    try:
        return EnvMap.from_dict(read_yaml(env_path)), cats, True
    except Exception as e:  # noqa: BLE001
        print(f"error: invalid baseline env map {env_path}: {e}", file=err)
        return None, None, False


def _load_changeset(baseline_root: Path, intent_root: Path, env_map, cats, err,
                    baseline_env_map=None, baseline_cats=None):
    """(removals, modifications, exit_code). Both None when the baseline is unusable.

    FAIL CLOSED. An unreadable or invalid baseline returns an error rather than
    "no removals" — silently reporting zero removals because the comparison
    broke is precisely the blindness this feature removes.

    THE BASELINE IS READ WITH THE BASELINE'S CATALOG. Both trees used to be
    parsed with today's, which makes a legitimate change unrepresentable: replace
    a firewall, and `main`'s intents name a serial the new catalog no longer
    declares, so the baseline is "invalid" and the whole run fails closed. The
    pipeline could not apply a firewall replacement at all (measured 2026-08-10).

    A baseline is a PAST STATE. It was valid under the catalog it shipped with,
    and judging it by today's is the same category error as an evidence bundle
    citing the ticket that authorised the previous version of a rule.

    `baseline_*` default to the current ones, so a caller that has no archived
    catalog behaves exactly as before.
    """
    baseline_env_map = env_map if baseline_env_map is None else baseline_env_map
    baseline_cats = cats if baseline_cats is None else baseline_cats
    from fwgitops.evidence import sha256_file
    from fwgitops.removal import Removal, find_modifications, find_removals

    if not baseline_root.exists():
        print(f"error: baseline intent tree not found: {baseline_root}", file=err)
        return None, None, 1

    def index(root: Path, strict: bool, _env_map=None, _cats=None):
        _env_map = env_map if _env_map is None else _env_map
        _cats = cats if _cats is None else _cats
        out: Dict[Tuple[str, str], Removal] = {}
        for path in discover_intents(root):
            try:
                doc = read_yaml(path)
                req = load_intent(doc, env_map=_env_map, **_cats)
            except Exception as e:  # noqa: BLE001
                if strict:
                    print(f"error: baseline intent {_display_path(path)} is unreadable "
                          f"({e}). Refusing to report removals from a baseline that "
                          f"cannot be fully parsed. If the baseline predates a "
                          f"catalog change — a replaced firewall, a renamed folder "
                          f"— pass --baseline-catalog pointing at the catalog that "
                          f"shipped WITH it.", file=err)
                    raise
                continue
            kind = doc.get("kind")
            out[(kind, req.metadata.id)] = Removal(
                kind=kind, req_id=req.metadata.id, request=req,
                # RELATIVE TO ITS OWN ROOT, not the CWD. The baseline tree is a
                # scratch directory (`git archive` into /tmp), so `_display_path`
                # would put `/tmp/base-intent/prod/…` into the evidence record —
                # a path that has never existed in the repository and cannot be
                # looked up by anyone reading the bundle later.
                path=str(Path(path).relative_to(root)),
                sha256=sha256_file(path))
        return out

    try:
        base = index(baseline_root, strict=True,
                     _env_map=baseline_env_map, _cats=baseline_cats)
    except Exception:  # noqa: BLE001 - already reported above
        return None, None, 2
    current = index(intent_root, strict=False)
    return find_removals(base, current.keys()), find_modifications(base, current), 0


def _id_matches_filename(path, request) -> Optional[str]:
    """A problem string when the file name and `metadata.id` disagree, else None.

    THE ID IS THE IDENTITY EVERYWHERE DOWNSTREAM: it names the rule in SCM, it is
    the evidence path (`evidence/<scope>/<id>.json`), and it is what
    `fwgitops where` searches. The FILE NAME is what a human looks under.

    They drifted once, live: `REQ-2026-0813.yaml` declared `id: REQ-2026-0812`
    and nothing rejected it. The rule applied and pushed as REQ-2026-0812, its
    evidence landed at `REQ-2026-0812.json`, and `fwgitops where REQ-2026-0813`
    — the id a human would type, having read the filename — returned nothing.
    The incident-response command cannot find a rule this repository authorised.

    Two files could also silently claim one id, so the later one overwrites the
    earlier one's evidence. Discovery skips `*.example.*`, so documentation files
    never reach this.
    """
    stem = Path(path).stem
    got = request.metadata.id
    if stem == got:
        return None
    return (f"metadata.id is {got!r} but the file is named {stem!r}. They must "
            f"match: the id names the rule in SCM and the evidence file, while "
            f"the file name is what a human searches. Rename the file to "
            f"{got}.yaml, or change metadata.id to {stem!r} — whichever is the "
            f"one you meant.")


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
            ar = load_intent(doc, env_map=env_map, **cats)
            mismatch = _id_matches_filename(path, ar)
            if mismatch:
                problems.append(f"{rel}: {mismatch}")
                continue
            ch = compile_any(ar, env_map)
            # EVERY kind, not just AccessRequest. This used to keep rules only,
            # which is right for evidence (bundles are rules-only) and WRONG for
            # drift: the declared set then contained no interfaces, zones or
            # routes, so every locally-defined Day-1 object in SCM was reported
            # as "present in SCM, neither declared nor a known baseline object".
            #
            # It stayed hidden because the two cases that would have shown it
            # were each blocked by something else: device-scope snapshots failed
            # outright until v1.34.2, and at folder scope the zones and routers
            # were inherited, which drift skips. Callers that want one kind
            # filter for themselves — `run_enrich` already does.
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
    snapshot_path: Optional[Path] = None,
    *,
    state_snapshot_paths: Optional[List[Path]] = None,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    record_violations: Optional[Path] = None,
    run_url: Optional[str] = None,
    out=None,
    err=None,
) -> int:
    """Detect drift: declared intents vs a snapshot of SCM's actual config.

    The snapshot is a JSON/YAML list of {folder, name, tags} (what a folder rule
    read returns). Reports UNMANAGED (added outside GitOps), ORPHANED (managed but
    no longer declared), and MALFORMED rules. Exit: 0 clean · 1 usage · 2 invalid
    intent · 3 drift found.
    """
    from fwgitops.drift import (
        ActualObject,
        ActualRule,
        declared_state,
        detect_drift,
        detect_object_drift,
    )

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code

    if snapshot_path is None and not state_snapshot_paths:
        print("error: pass --snapshot (tag-based) and/or --state-snapshot (state-based)",
              file=err)
        return 1

    drifted = False

    # ── State-based drift, for every kind that cannot carry tags ──────────
    # One loop over the registry: a kind declaring drift_engine="state" is
    # covered the moment it registers. This used to be hardcoded to zones, so
    # InterfaceRequest declared state-based drift while nothing wired it.
    if state_snapshot_paths:
        env_map = EnvMap.from_dict(read_yaml(env_map_path))
        compiled_all = [c for _, _, c in items]
        actual_by_kind: Dict[str, List[ActualObject]] = {}
        for path in state_snapshot_paths:
            rows, code = _read_snapshot_rows(path, err)
            if rows is None:
                return code
            kind = _kind_of_snapshot(rows, path, err)
            if kind is None:
                return 1
            for i, x in enumerate(rows):
                # WHERE THE OBJECT IS DEFINED, which is not always a folder. A
                # device-scope OVERRIDE is defined at `device:<serial>` and the
                # row carries `device` with no `folder` at all — so requiring
                # `folder` rejected every device snapshot, and state drift was
                # therefore never checked for a firewall's own overrides.
                #
                # An INHERITED object read at device scope carries both: the
                # ancestor in `folder` and `device:<serial>` in `scope`. Keeping
                # the defining location here is what lets `is_inherited`
                # (scope != folder) stay correct at either scope.
                defining = None
                if isinstance(x, dict):
                    if x.get("folder"):
                        defining = str(x["folder"])
                    elif x.get("device"):
                        defining = f"device:{x['device']}"
                if defining is None or not isinstance(x, dict) or "name" not in x:
                    print(f"error: {path} [{i}] must have 'name' and one of "
                          f"'folder' / 'device' — a device-scope override is defined "
                          f"at device:<serial>, not in a folder", file=err)
                    return 1
                fields = {k: v for k, v in x.items() if k not in ("id", "tfid", "scope")}
                actual_by_kind.setdefault(kind, []).append(ActualObject(
                    kind=kind, folder=defining, name=str(x["name"]),
                    fields=fields,
                    # SCM returns the DEFINING location; `scope` records which was
                    # queried. They differ for an inherited object.
                    scope=str(x["scope"]) if x.get("scope") else None,
                ))

        for kind, actual in sorted(actual_by_kind.items()):
            handler = REGISTRY[kind]
            # `baseline_zones` names objects that legitimately pre-date GitOps.
            # It is zone vocabulary, so only zones get an allowlist; for other
            # kinds an undeclared local object is unaccounted for by definition.
            baseline = (env_map.baseline_zones_by_folder()
                        if kind == "ZoneRequest" else None)
            # ONLY THE SCOPES THIS SNAPSHOT COVERS. The caller checks one root
            # at a time — the scheduled job loops `terraform/*/` — so a device
            # snapshot contains nothing about `prod-edge`. Comparing the WHOLE
            # declared set against it reported every other scope's objects as
            # "declared in Git, absent from SCM": drift that is not there, on a
            # firewall that is perfectly in step.
            #
            # The queried scope is on every row (`scope`), so the snapshot itself
            # says what it covers. An object in a scope nobody snapshotted is not
            # evidence of anything.
            covered = {a.scope_folder for a in actual}
            declared_all = declared_state(handler, of_kind(compiled_all, kind))

            # FOLDER INTERFACE VARIABLES ARE DECLARED TOO — in
            # catalog/interfaces.yaml rather than in an intent. `fwgitops
            # folder-interfaces` writes them and Terraform manages them, so
            # without this the check reported `prod-edge/$eth-dmz` as "present in
            # SCM, neither declared nor a known baseline object" on every run.
            # Built from the same catalog method that emits them, so the two
            # cannot disagree about their shape.
            ifcat = cats.get("interface_catalog")
            if kind == "InterfaceRequest" and ifcat is not None:
                for scope in covered:
                    if scope.startswith("device:"):
                        continue          # a `$`-variable is a FOLDER object
                    for name, fields in ifcat.folder_variable_objects(scope).items():
                        declared_all.setdefault((scope, name), fields)

            declared = {k: v for k, v in declared_all.items() if k[0] in covered}
            report = detect_object_drift(declared, actual, baseline=baseline)
            print(f"{kind}: {report.summary()}", file=out)
            drifted = drifted or not report.is_clean

    if snapshot_path is None:
        return 3 if drifted else 0

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
        # SCM returns the field as `tag`; this parser only read `tags`, so a
        # snapshot straight from the API would have shown every rule as
        # UNTAGGED — and therefore unmanaged, including our own. `tagsweep`
        # already accepts both spellings; this now matches it.
        tags = x.get("tags")
        if tags is None:
            tags = x.get("tag")
        # `scope` is the folder that was QUERIED — `snapshot` stamps it. Without
        # it every inherited rule reads as locally-defined and therefore as
        # drift, which is what the first live run did.
        scope = x.get("scope")
        actual.append(ActualRule(folder=str(x["folder"]), name=str(x["name"]),
                                  tags=tuple(tags or []),
                                  scope=str(scope) if scope else None))

    report = detect_drift(
        of_kind([ch for _, _, ch in items], "AccessRequest"), actual
    )
    print(report.summary(), file=out)

    # RECORD THE FINDING, not just the failure. Detection failed the run and
    # left nothing behind: the classification existed here and never reached
    # disk, so a violation could not be aged, counted, routed into a follow-up
    # process, or produced for an assessor later. CI logs expire.
    if record_violations is not None:
        from fwgitops import violations as _v

        found = [
            {"cls": cls, "kind": "security-rule",
             "scope": r.scope or r.folder, "name": r.name, "tags": list(r.tags)}
            for cls, rules in (("unmanaged", report.unmanaged),
                               ("orphaned", report.orphaned),
                               ("malformed", report.malformed))
            for r in rules
        ]
        # Only scopes this run actually READ may have their findings resolved —
        # a scope we could not look at must not be reported as clean.
        checked = sorted({(r.scope or r.folder) for r in actual})
        changed = _v.reconcile(found=found, existing=_v.load(record_violations),
                               root=record_violations, run_url=run_url or "",
                               scopes_checked=checked)
        for path in _v.write(changed):
            print(f"violation record: {path}", file=out)
        print(_v.summarise(
            [rec for rec in _v.load(record_violations).values()]), file=out)

    return 0 if (report.is_clean and not drifted) else 3



FOLDER_VARS_FILENAME = "interface_vars.auto.tfvars.json"


def run_folder_interfaces(
    out_root: Path,
    *,
    interface_catalog_path: Path = Path("catalog/interfaces.yaml"),
    folders_path: Path = Path("catalog/folders.yaml"),
    write: bool = True,
    out=None,
    err=None,
) -> int:
    """Materialise each folder's `$`-interface VARIABLES from the catalog.

    WHY THIS IS NOT `compile`. `compile` turns INTENTS into desired state, and
    these are not intents — no requester asks for them, and a requester must not
    be able to conjure a physical port by filing one. They are platform topology,
    declared in `catalog/interfaces.yaml`, which is platform-maintained and
    changed by PR.

    More practically: a GREENFIELD folder has no intents at all, and `compile`
    returns early on an empty intent tree. The folder needs its variables BEFORE
    the first intent can bind one, so driving this off intents is circular.

    WHY NOT BOOTSTRAP EITHER. `terraform/bootstrap-scm-folder` is run-once with
    LOCAL, gitignored state, so its state lives on exactly one machine. Creating
    interface variables there makes every later addition a manual apply from that
    machine, outside the pipeline: no PR plan, no risk classification, no
    evidence bundle, and invisible to drift (which is registry-driven per kind
    and would own none of it). Adding an interface is infrequent, not run-once —
    the two must not be filed together.

    So this writes into the folder's CI-owned root, next to its zones and rules,
    sharing the same remote state.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        from fwgitops.catalog import CatalogError, FolderHierarchy, InterfaceCatalog
        from fwgitops.io import read_yaml as _ry
        cat = InterfaceCatalog.from_dict(_ry(interface_catalog_path))
        hier = FolderHierarchy.from_dict(_ry(folders_path))
    except FileNotFoundError as e:
        print(f"error: catalog not found: {e}", file=err)
        return 1
    except Exception as e:  # noqa: BLE001 - CatalogError or a YAML failure
        print(f"error: invalid catalog: {e}", file=err)
        return 1

    # FAIL CLOSED before writing anything. A folder variable holds ONE
    # default_value; if the folder's own firewalls resolve the role to different
    # ports, no single value is right, and choosing one sends the other
    # firewall's traffic out the wrong wire while every check stays green.
    conflicts = cat.create_in_conflicts(hier.devices_of)
    if conflicts:
        print(f"REJECTED — {len(conflicts)} interface catalog conflict(s); nothing written:",
              file=err)
        for c in conflicts:
            print(f"  - {c}", file=err)
        return 2

    written: List[Path] = []
    for folder in sorted(hier.targetable_folders()):
        variables = cat.folder_variables(folder)
        if not variables:
            continue
        # The module merges `folder_interfaces` with the compiled `interfaces`
        # into one for_each. That merge is only safe because the key spaces are
        # disjoint, and this is where that holds: a folder-scope object is a
        # `$`-prefixed VARIABLE, a device-scope one is a physical port. Asserted
        # rather than assumed — a collision would silently drop one side, and
        # `merge` gives no diagnostic.
        bad = [n for n in variables if not n.startswith("$")]
        if bad:
            print(f"REJECTED — folder {folder}: interface variable(s) {bad} are not "
                  f"`$`-prefixed. A folder-scope interface is a VARIABLE; a bare port "
                  f"name here would collide with a device-scope interface of the same "
                  f"name and one would be silently dropped.", file=err)
            return 2
        # Built by the catalog so drift recognises exactly what is written here.
        payload = {"folder_interfaces": cat.folder_variable_objects(folder)}
        target = out_root / folder / FOLDER_VARS_FILENAME
        problems = check_object_attributes(
            target.parent, "folder_interfaces", payload["folder_interfaces"])
        if problems:
            print(f"REJECTED — Terraform contract problem(s) for {folder}; nothing written:",
                  file=err)
            for p in problems:
                print(f"  - {p}", file=err)
            return 2
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dumps_payload(payload), encoding="utf-8")
        written.append(target)

    verb = "wrote" if write else "would write"
    print(f"OK — {verb} {len(written)} folder interface file(s):", file=out)
    for t in written:
        print(f"  - {t}", file=out)
    return 0


def run_scaffold_root(
    out_root: Path,
    *,
    folder: Optional[str] = None,
    device: Optional[str] = None,
    device_folder: Optional[str] = None,
    check: bool = False,
    sync: bool = False,
    module_dir: Optional[Path] = None,
    out=None,
    err=None,
) -> int:
    """Create a Terraform ROOT for a scope, or verify/refresh existing ones.

    A root is almost all boilerplate that must mirror the module ATTRIBUTE FOR
    ATTRIBUTE, because Terraform discards an undeclared object attribute at the
    module boundary silently (ADR-0004, HOLE 3). Hand-copying ~260 lines for a
    new folder was the last manual step between "add a folder to the catalog"
    and a working firewall.

    Three modes:
      (default)  create a new root; REFUSES to overwrite an existing one
      --sync     regenerate variables.tf for every existing root
      --check    report roots whose variables.tf is stale; write nothing
    """
    from fwgitops.scaffold import (
        BACKEND_TF, ScaffoldError, Scope, backend_example, render_main,
        render_variables,
    )
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    module_dir = module_dir or (out_root / "modules" / "security_folder")
    if not module_dir.is_dir():
        print(f"error: module not found: {module_dir}", file=err)
        return 1

    def _roots() -> List[Path]:
        found = []
        for main in sorted(out_root.glob("*/main.tf")):
            if f'source = "{MODULE_SOURCE_LITERAL}"' in main.read_text(encoding="utf-8"):
                found.append(main.parent)
        return found

    try:
        if check or sync:
            if folder or device:
                print("error: --check/--sync operate on every existing root; "
                      "do not also name one", file=err)
                return 1
            stale: List[Path] = []
            for root in _roots():
                current = (root / "variables.tf")
                scope_folder = _root_folder_default(current)
                want = render_variables(module_dir, scope_folder)
                if current.read_text(encoding="utf-8") != want:
                    stale.append(root)
                    if sync:
                        current.write_text(want, encoding="utf-8")
            if sync:
                print(f"OK — synced {len(stale)} root(s) to the module:", file=out)
                for r in stale:
                    print(f"  - {r}/variables.tf", file=out)
                return 0
            if stale:
                print(f"STALE — {len(stale)} root(s) no longer mirror the module. "
                      f"Terraform would DISCARD the difference silently:", file=err)
                for r in stale:
                    print(f"  - {r}/variables.tf", file=err)
                print("Run `fwgitops scaffold-root --sync`.", file=err)
                return 2
            print(f"OK — {len(_roots())} root(s) mirror the module", file=out)
            return 0

        if bool(folder) == bool(device):
            print("error: name exactly one of --folder or --device", file=err)
            return 1
        scope = Scope(folder=folder, device=device)
        scope_folder = folder if folder else device_folder
        if not scope_folder:
            print("error: --device also needs --device-folder (the CONTAINING folder). "
                  "Tags are folder objects even when the interface is a device "
                  "override, and `folder=<serial>` is rejected by SCM.", file=err)
            return 1

        root = out_root / scope.dirname
        if root.exists() and any(root.glob("*.tf")):
            # Never overwrite. main.tf carries hand-written reasoning, and a
            # root's backend points at real state — regenerating one silently is
            # how a state file gets orphaned.
            print(f"error: {root} already exists. Use --sync to refresh variables.tf; "
                  f"main.tf is written once, deliberately.", file=err)
            return 1

        if scope.folder:
            header = (f'# Per-folder root module for SCM folder "{scope.folder}". One root ==\n'
                      "# one state (design Arch-2). `terraform plan` here is the PR preview\n"
                      "# + drift detector.\n")
        else:
            header = (f"# Per-scope root module for FIREWALL {scope.device}. One root == one\n"
                      "# state (design Arch-2).\n"
                      "#\n"
                      "# SEPARATE FROM ITS FOLDER'S ROOT on purpose: a device-scope write does\n"
                      "# not EDIT the inherited object, it creates a per-device OVERRIDE with\n"
                      "# its own id (spike/device-override-probe). Two objects means two\n"
                      f"# states; sharing a root would let one scope's plan destroy the\n"
                      "# other's overrides.\n")

        root.mkdir(parents=True, exist_ok=True)
        (root / "variables.tf").write_text(render_variables(module_dir, scope_folder),
                                           encoding="utf-8")
        (root / "main.tf").write_text(render_main(module_dir, scope, header=header),
                                      encoding="utf-8")
        (root / "backend.tf").write_text(BACKEND_TF, encoding="utf-8")
        (root / "backend.hcl.example").write_text(backend_example(scope), encoding="utf-8")
    except ScaffoldError as e:
        print(f"error: {e}", file=err)
        return 2

    print(f"OK — scaffolded {root}:", file=out)
    for f in sorted(root.glob("*")):
        print(f"  - {f}", file=out)
    print("", file=out)
    print("NEXT, and none of it is optional:", file=out)
    print(f"  1. ./terraform/make-backend.sh {scope.dirname}   # writes backend.hcl", file=out)
    print(f"  2. terraform -chdir={root} init -backend-config=backend.hcl", file=out)
    print("  3. commit the generated .terraform.lock.hcl — an unlocked root can "
          "select a different provider build", file=out)
    return 0


#: The literal every root uses to call the shared module. Roots are found by it.
MODULE_SOURCE_LITERAL = "../modules/security_folder"


def _root_folder_default(variables_tf: Path) -> str:
    """The SCM folder an existing root defaults its `folder` variable to.

    Read back rather than re-derived from the directory name: a DEVICE root is
    `device-<serial>` on disk but its `folder` is the CONTAINING folder, and
    guessing would put a serial where SCM rejects one.
    """
    import re as _re
    text = variables_tf.read_text(encoding="utf-8")
    m = _re.search(r'variable\s+"folder"\s*\{.*?default\s*=\s*"([^"]+)"', text, _re.S)
    if not m:
        raise __import__("fwgitops.scaffold", fromlist=["ScaffoldError"]).ScaffoldError(
            f"{variables_tf} has no default for `folder`; cannot tell which SCM folder "
            f"this root is for")
    return m.group(1)


def run_verify_catalog(
    *,
    folders_path: Path = Path("catalog/folders.yaml"),
    interface_catalog_path: Path = Path("catalog/interfaces.yaml"),
    session=None,
    out=None,
    err=None,
) -> int:
    """Verify catalog/folders.yaml against SCM's real hierarchy. READ-ONLY.

    The catalog is declared rather than read live so the compiler and classifier
    stay pure — the same intents always compile to the same output. Purity buys
    determinism, not truth, and nothing was checking the truth.

    Exit 2 on a BLOCKING divergence: the catalog claiming something SCM
    contradicts, for an object an intent could actually name. A stale entry
    already marked `targetable: false` is reported and does not fail — the
    operator has said "do not use this", which is the acknowledgement, and
    failing anyway would just train people to ignore the check.

    NOT reported: objects in SCM the catalog does not mention. Prisma Access
    built-ins are deliberately absent because this platform does not manage
    them, and a check that cries wolf every run gets ignored.
    """
    from fwgitops.catalog import FolderHierarchy, InterfaceCatalog
    from fwgitops.catalogcheck import compare, compare_interfaces, parse_live
    from fwgitops.io import read_yaml as _ry

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        hier = FolderHierarchy.from_dict(_ry(folders_path))
        ifcat = InterfaceCatalog.from_dict(_ry(interface_catalog_path))
    except FileNotFoundError as e:
        print(f"error: catalog not found: {e}", file=err)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"error: invalid catalog: {e}", file=err)
        return 1

    if session is None:
        from fwgitops.scmapi import ScmCredentials, ScmSession
        try:
            session = ScmSession(ScmCredentials.from_env())
        except Exception as e:  # noqa: BLE001 - missing/!invalid credentials
            print(f"error: cannot reach SCM: {e}", file=err)
            return 1

    try:
        payload = session.request("GET", "/config/setup/v1/folders",
                                  params={"limit": 500})
    except Exception as e:  # noqa: BLE001 - any transport/API failure
        print(f"error: reading the SCM hierarchy failed: {e}", file=err)
        return 1

    live = parse_live(payload.get("data", []))
    if not live:
        # Fail closed. An empty hierarchy would make every declared folder look
        # absent and turn this into a blanket failure, or — worse, if the
        # comparison were ever inverted — a blanket pass.
        print("error: SCM returned no folders at all; refusing to compare against "
              "an empty hierarchy", file=err)
        return 1

    findings = compare(hier, live) + compare_interfaces(ifcat, hier, live)
    blocking = [f for f in findings if f.blocking]
    noted = [f for f in findings if not f.blocking]

    for f in noted:
        print(f"NOTE  {f}", file=out)
    if blocking:
        print(f"REJECTED — the catalog contradicts SCM in {len(blocking)} place(s):",
              file=err)
        for f in blocking:
            print(f"  - {f}", file=err)
        return 2

    print(f"OK — catalog matches SCM ({len(live)} live entries, "
          f"{len(noted)} note(s) above)", file=out)
    return 0


def run_device_sync(*, session=None, out=None, err=None) -> int:
    """Is each FIREWALL running what SCM holds? READ-ONLY.

    Drift detection compares Git against SCM. Nothing compared SCM against the
    DEVICE — so a change could be applied in SCM and never reach the firewall,
    with Git and SCM agreeing while the device runs something else. Silent,
    persistent, and the next successful push by anyone applies it.

    Exit 2 when any device is behind, never-pushed, or unreadable. Unlike the
    catalog's cosmetic findings this is not a note: config that exists in SCM and
    is not enforced on the firewall is the platform's core claim being false.
    """
    from fwgitops.devicesync import compare, latest_committed, running_by_device

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if session is None:
        from fwgitops.scmapi import ScmCredentials, ScmSession
        try:
            session = ScmSession(ScmCredentials.from_env())
        except Exception as e:  # noqa: BLE001
            print(f"error: cannot reach SCM: {e}", file=err)
            return 1
    try:
        devices = session.request("GET", "/config/setup/v1/devices",
                                  params={"limit": 200}).get("data", []) or []
        running = running_by_device(
            session.request("GET", "/config/operations/v1/config-versions/running"))
        latest: Dict[str, Optional[int]] = {}
        for folder in sorted({d.get("folder") for d in devices if d.get("folder")}):
            vs = session.request("GET", "/config/operations/v1/config-versions/candidate",
                                 params={"folder": folder}).get("data", []) or []
            latest[folder] = latest_committed(vs)
    except Exception as e:  # noqa: BLE001 - any transport/API failure
        print(f"error: reading SCM config versions failed: {e}", file=err)
        return 1

    if not devices:
        # Fail closed: no devices could mean a healthy empty tenant OR a broken
        # read, and reporting "all in sync" for the second is the blindness this
        # command exists to remove.
        print("error: SCM returned no devices; refusing to report sync status "
              "against an empty inventory", file=err)
        return 1

    results = compare(devices, running, latest)
    problems = [r for r in results if r.is_problem]
    notes = [r for r in results if getattr(r, "is_note", False)]
    for r in sorted(results, key=lambda x: x.serial):
        ver = f"v{r.running_version}" if r.running_version is not None else "v?"
        want = f"v{r.latest_version}" if r.latest_version is not None else "v?"
        print(f"  {r.serial:20} {r.state:13} running={ver:5} committed={want:5} "
              f"folder={r.folder}", file=out)
    for r in notes:
        print(f"NOTE  {r.serial}: {r.detail}", file=out)
    if problems:
        print(f"OUT OF SYNC — {len(problems)} of {len(results)} device(s):", file=err)
        for r in problems:
            print(f"  - {r.serial}: {r.detail}", file=err)
        return 2
    print(f"OK — {len(results)} device(s) running the newest committed config"
          + (f", {len(notes)} note(s) above" if notes else ""), file=out)
    return 0


def run_apply_order(out_root: Path, *, out=None, err=None) -> int:
    """Print Terraform root directories in APPLY order (ADR-0002's chain).

    The pipeline consumed `for dir in terraform/*/`, i.e. glob order. On this
    tenant `device-<serial>` happens to sort before `prod-edge`, so interfaces
    happened to apply before the routes and rules that depend on them — correct
    by accident, and it would silently invert on a rename.

    A root's position is the EARLIEST kind it contains: a root holding
    interfaces must precede one holding zones. Roots with no emitted tfvars are
    skipped, not ordered, since there is nothing to apply.

    FAILS CLOSED when no total order exists. If root A holds a kind that depends
    on a kind in root B, while B also holds a kind depending on one in A, no
    ordering of whole-root applies can satisfy both — that needs per-kind applies
    and is a real design change, so it is reported rather than papered over with
    an arbitrary sequence.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    order = {k: i for i, k in enumerate(kind_apply_order())}
    by_file = {h.tfvars_filename: h.kind for h in REGISTRY.values()}

    roots: Dict[str, set] = {}
    for d in sorted(p for p in out_root.glob("*/") if p.is_dir()):
        if d.name in ("modules",) or d.name.startswith(("bootstrap-", "github-oidc")):
            continue
        kinds = {by_file[f.name] for f in d.glob("*.auto.tfvars.json") if f.name in by_file}
        if kinds:
            roots[d.name] = kinds

    if not roots:
        return 0

    ordered = sorted(roots, key=lambda r: (min(order[k] for k in roots[r]), r))
    pos = {r: i for i, r in enumerate(ordered)}

    violations = []
    for root, kinds in roots.items():
        for kind in kinds:
            for dep in REGISTRY[kind].depends_on_kinds:
                for other, other_kinds in roots.items():
                    if other != root and dep in other_kinds and pos[other] > pos[root]:
                        violations.append(f"{root}/{kind} needs {dep}, which applies later in {other}")
    if violations:
        print("error: no whole-root apply order satisfies the kind dependencies:", file=err)
        for v in sorted(set(violations)):
            print(f"  - {v}", file=err)
        print("  Kinds are interleaved across roots; this needs per-kind applies.", file=err)
        return 2

    for r in ordered:
        print(r, file=out)
    return 0


def run_snapshot(
    kind: str,
    folder: str,
    out_path: Path,
    *,
    device: Optional[str] = None,
    out=None,
    err=None,
) -> int:
    """Read a folder's live objects of one KIND from SCM. READ-ONLY.

    Driven off `KindHandler.state_api_path`, so a kind that registers one is
    snapshottable with no code here — the alternative was a hand-written command
    per kind, which is the sprawl ADR-0001's registry exists to prevent.

    With `device=<serial>`, reads a FIREWALL's scope instead. A firewall is the
    last level of the SCM hierarchy but is addressed `device=`, never `folder=`
    (which returns 400) — and its scope key is `device:<serial>`, matching what
    `drift` and the classifier build. Without this, a device-scoped change could
    never be compared against live state, so `interface_becomes_addressed` and
    friends would silently never fire and report LOW.

    Records the QUERIED scope as `scope` on every row. SCM returns the folder an
    object is DEFINED in, which for an inherited object is an ancestor — without
    `scope`, drift cannot tell "this folder owns it" from "this folder inherits
    it", and reports every inherited object as unexpected. That was 7 false
    positives against the live tenant.
    """
    import json

    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    handler = REGISTRY.get(kind)
    if handler is None or not handler.state_api_path:
        snappable = ", ".join(sorted(h.kind for h in kinds_with_state_api()))
        print(f"error: {kind!r} has no state API path; snapshottable kinds: {snappable}",
              file=err)
        return 1
    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        scope_param = {"device": device} if device else {"folder": folder}
        payload = session.request(
            "GET", handler.state_api_path, params={**scope_param, "limit": 200}
        )
    except ScmConfigError as e:
        print(f"error: SCM credentials not usable: {e}", file=err)
        return 1
    except ScmApiError as e:
        print(f"error: SCM read failed: {e}", file=err)
        return 1

    rows = []
    for obj in payload.get("data", []):
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        row = {k: v for k, v in obj.items() if k not in ("id", "tfid")}
        if device:
            row.setdefault("device", device)
        else:
            row.setdefault("folder", folder)
        # The scope KEY, not the raw name — must match Scope.key, which is what
        # drift and the classifier look up by.
        row["scope"] = f"device:{device}" if device else folder
        # Stamp the kind so drift attributes the snapshot without guessing.
        row["kind"] = kind
        rows.append(row)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    label = f"device {device!r}" if device else f"folder {folder!r}"
    inherited = sum(1 for r in rows if r.get("folder") not in (None, folder))
    print(f"wrote {len(rows)} {kind} object(s) for {label} to {out_path} "
          f"({inherited} inherited from an ancestor)", file=out)
    return 0


def _kind_of_snapshot(rows: List[Any], path: Path, err) -> Optional[str]:
    """Which registered kind a snapshot holds, from its `kind` field.

    `fwgitops snapshot` stamps it. Guessing from the object shape instead would
    silently mis-attribute a snapshot to the wrong kind, and drift would then
    compare it against the wrong declared set.
    """
    kinds = {x.get("kind") for x in rows if isinstance(x, dict) and x.get("kind")}
    if len(kinds) == 1:
        kind = kinds.pop()
        if kind in REGISTRY:
            return kind
        print(f"error: {path}: unknown kind {kind!r}; known: {sorted(REGISTRY)}", file=err)
        return None
    if not kinds:
        print(f"error: {path}: no `kind` field — regenerate with `fwgitops snapshot`",
              file=err)
        return None
    print(f"error: {path}: mixed kinds {sorted(kinds)}; one snapshot per kind", file=err)
    return None


def _read_snapshot_rows(path: Path, err):
    """Read a JSON/YAML snapshot into a list of dicts. Returns (rows, exit_code)."""
    if not path.is_file():
        print(f"error: SCM snapshot not found: {path}", file=err)
        return None, 1
    try:
        raw = read_yaml(path)
    except Exception as e:  # noqa: BLE001
        print(f"error: could not read snapshot {path}: {e}", file=err)
        return None, 1
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print(f"error: snapshot {path} must be a list of objects", file=err)
        return None, 1
    return rows, 0


#: Exactly what a push may disclose in a committed bundle. `admins` is absent by
#: design — it defaults to `SCM_CLIENT_ID`, a secret; `all_admins` carries the
#: audit-relevant half (was this break-glass?) without the identity.
#: `reason` joins them 2026-08-15. `push: null` had meant "nothing was staged",
#: which was the only way to get there while one folder existed. A second folder
#: added two more: a folder with no firewall beneath it (nothing to push TO), and
#: a push that was attempted and FAILED. An assessor could not tell a correct
#: decision from a breakage, which is the one thing this field exists to say.
_PUSH_EVIDENCE_KEYS = ("folder", "status", "job_id", "admin_count", "all_admins",
                       "reason")


def _write_push_record(record: Optional[Path], payload: Dict[str, Any], err) -> None:
    """Write a `--record` file for a push that did not happen.

    Best effort on purpose: the push decision has already been made correctly,
    so failing to write the note about it must not turn a correct run red. The
    note is for the audit trail, not for the control.
    """
    if record is None:
        return
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(
            {"scope_dir": payload.get("folder", ""), **payload},
            indent=2, sort_keys=True) + "\n")
    except OSError as e:  # noqa: BLE001
        print(f"warning: could not write push record {record}: {e}", file=err)


@dataclass(frozen=True)
class _RecordedPush:
    """A push outcome read back from `fwgitops push --record`.

    Only needs to answer `to_evidence()`, because that is the whole of what a
    bundle asks of a push.
    """

    payload: Dict[str, Any]

    def to_evidence(self) -> Dict[str, Any]:
        return dict(self.payload)


def _push_results(paths: Optional[List[Path]], err) -> Optional[Dict[str, Any]]:
    """`scope dirname -> PushResult`, from files written by `fwgitops push --record`.

    FAIL CLOSED. An unreadable or malformed record returns None (the caller
    exits 1) rather than an empty map. "No push happened" and "the record could
    not be read" produce the same `"push": null` in a bundle, and that is the
    one distinction this field exists to make — a silent fallback would let a
    broken record file quietly restore the very defect being fixed.

    A MISSING record is different, and legitimate: a scope with nothing staged
    is never pushed, so no file is written and its bundles keep `push: null`.
    Those bundles are also the ones `write_bundle_if_changed` leaves alone.
    """
    if not paths:
        return {}
    out: Dict[str, Any] = {}
    for p in paths:
        try:
            d = json.loads(Path(p).read_text())
            scope_dir = d.pop("scope_dir")
            # REPLAYED, NOT REBUILT. Reconstructing a `PushResult` here would
            # mean re-deriving the evidence shape from parts, and the part it
            # would need is the one deliberately absent: `admins` holds
            # `SCM_CLIENT_ID`. `push --record` already wrote the redacted shape,
            # so the record is carried through verbatim and there is exactly one
            # place that decides what a push discloses.
            for required in ("folder", "status"):
                if required not in d:
                    raise KeyError(required)
            # ALLOW-LIST, not passthrough. Replaying the file verbatim would
            # mean a record containing an identity puts it in a committed
            # bundle — `push --record` does not write one, but "the producer is
            # careful" is not a property of the consumer, and this consumer is
            # the last step before a public commit. Unknown keys are dropped
            # rather than rejected: a newer `push` adding a field should not
            # fail an older `evidence`.
            out[scope_dir] = _RecordedPush(
                {k: d[k] for k in _PUSH_EVIDENCE_KEYS if k in d})
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"error: push record {p} is unreadable ({e}). Refusing to write "
                  f"bundles that would claim nothing was pushed when the record "
                  f"simply could not be read.", file=err)
            return None
    return out


def run_evidence(
    intent_root: Path,
    env_map_path: Path,
    out_root: Path,
    *,
    status: str = "applied",
    tfvars_root: Path = Path("terraform"),
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    baseline_root: Optional[Path] = None,
    baseline_catalog_dir: Optional[Path] = None,
    change_message_path: Optional[Path] = None,
    approvers: Optional[List[str]] = None,
    pr_url: Optional[str] = None,
    push_records: Optional[List[Path]] = None,
    out=None,
    err=None,
) -> int:
    """Write a NIST-mapped evidence bundle per change, for EVERY kind (Phase 2).

    Compiles + classifies every intent, then assembles one bundle per change with
    the risk verdict, intent/tfvars hashes, and CI provenance (from GITHUB_* env),
    written to `out_root/<scope>/<req_id>.json`. This is the apply-path audit
    record — run it after apply/push; the workflow commits the folder.

    Until v1.36.0 this filtered to `AccessRequest`, so a route, zone or interface
    change produced NO audit record while the command reported success. The
    filter is gone: every registered kind is bundled, and a kind that cannot be
    bundled would now fail loudly rather than be skipped silently.

    A bundle whose change is unchanged is LEFT AS COMMITTED — see
    `evidence.write_bundle_if_changed`. Rewriting it would stamp a request nobody
    touched with the run that applied something else, and would turn
    `git log evidence/<scope>/<REQ>.json` into a log of applies rather than of
    changes to that request.

    Exit codes:  0 ok · 1 usage/IO/build error · 2 invalid intent.
    """
    import os
    from datetime import datetime, timezone

    from fwgitops.classify import PolicyContext
    from fwgitops.evidence import (
        STATUS_REMOVED,
        CIContext,
        EvidenceError,
        RemovalContext,
        build_bundle,
        sha256_file,
        write_bundle_if_changed,
    )

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code
    # NO EARLY RETURN ON AN EMPTY TREE. Deleting every intent leaves `items`
    # empty, and returning here would produce no record for the LARGEST POSSIBLE
    # removal while exiting 0 — the exact early-return that once let an empty
    # tree bypass the risk gate (`test_deleting_EVERY_intent_is_still_classified`).
    # The same shape, one stage further along.
    if not items and baseline_root is None:
        print(f"no intent files found under {intent_root}", file=out)
        return 0

    compiled = [ch for _, _, ch in items]
    policy = PolicyContext.from_changes(of_kind(compiled, "AccessRequest"))
    # APPROVALS ARE PASSED IN, never discovered — fetching them would put a
    # GitHub API call inside the record builder. Absent, the bundle declines to
    # claim CM-5 rather than claiming it over an empty list.
    ci = CIContext.from_env(os.environ, approvers=tuple(approvers or ()),
                            pr_url=pr_url)

    # WHAT REACHED SCM, per scope. Written by `fwgitops push --record`; keyed by
    # Terraform root directory because that is how a change knows its own scope.
    pushes = _push_results(push_records, err)
    if pushes is None:
        return 1
    # SAY WHEN NOTHING WAS SUPPLIED. `push: null` means either "this scope was
    # not pushed" or "nobody told me", and only the first is a fact about the
    # change. The bundle cannot distinguish them, so the RUN does — the same
    # reason `--baseline`'s absence is announced rather than left to look like
    # "no removals".
    if not push_records and status in ("applied", STATUS_REMOVED):
        print("note: no --push-record, so every bundle will record `push: null` — "
              "proving only that Terraform applied the change, not that it reached "
              "SCM. The apply workflow passes one per scope.", file=out)

    now = datetime.now(timezone.utc)
    written: List[Path] = []
    unchanged: List[Path] = []
    removed: List[Path] = []
    for path, ar, ch in items:
        handler = handler_for_request(ar)
        # The tfvars file this kind writes, in this object's OWN scope — a
        # device-scoped interface hashes `terraform/device-<serial>/…`, not the
        # folder's. Getting that wrong would hash a real file belonging to a
        # different scope, which is worse than hashing nothing.
        tfvars = tfvars_root / handler.scope_of(ch).dirname / handler.tfvars_filename
        try:
            bundle = build_bundle(
                request=ar, compiled=ch, handler=handler, status=status, generated_at=now,
                intent_sha256=sha256_file(path), intent_path=_display_path(path),
                tfvars_sha256=sha256_file(tfvars) if tfvars.is_file() else None,
                risk=handler.classify(ch, policy=policy), ci=ci,
                push=pushes.get(handler.scope_of(ch).dirname),
            )
        except EvidenceError as e:
            print(f"error: could not build evidence for {handler.name_of(ch)}: {e}", file=err)
            return 1
        path, is_new = write_bundle_if_changed(bundle, out_root)
        (written if is_new else unchanged).append(path)

    # ── REMOVALS ──────────────────────────────────────────────────────────
    # A deleted intent is absent from `items`, so without a baseline there is
    # nothing here to build a record from — and until v1.37.0 that meant a
    # removal produced no audit record at all, while `classify` had been tiering
    # it since v1.30.0. Assessed but unrecorded is a strange place to stop.
    #
    # The record is a TOMBSTONE WRITTEN IN PLACE, over the object's existing
    # bundle (ADR-0008 amendment, Q1a): one file per request is what makes
    # `git log evidence/<scope>/<REQ>.json` that request's whole life, create to
    # removal. The removed object is embedded from the baseline, so the record
    # still says WHAT went — reading git history is not required.
    if baseline_root is not None:
        env_map = EnvMap.from_dict(read_yaml(env_map_path))
        b_env, b_cats, b_ok = _baseline_catalogs(baseline_catalog_dir, env_map_path, err)
        if not b_ok:
            return 1
        removals, _mods, code = _load_changeset(baseline_root, intent_root, env_map,
                                                cats, err,
                                                baseline_env_map=b_env,
                                                baseline_cats=b_cats)
        if removals is None:
            return code
        trailers = _removes_trailers(change_message_path, err)
        if trailers is None:
            return 1
        problems = removal_ticket_problems(removals, trailers)
        if problems:
            print(f"REJECTED — {len(problems)} removal(s) without an authorising "
                  f"ticket:", file=err)
            for p in problems:
                print(f"  - {p}", file=err)
            return 2
        for r in removals:
            handler = handler_for_request(r.request)
            try:
                gone = handler.compile(r.request, env_map)
            except CompileError as e:
                print(f"error: could not compile removed {r.req_id} from the baseline "
                      f"to record what was destroyed: {e}", file=err)
                return 1
            try:
                bundle = build_bundle(
                    request=r.request, compiled=gone, handler=handler,
                    status=STATUS_REMOVED, generated_at=now,
                    intent_path=str(Path(_display_path(intent_root)) / r.path),
                    intent_sha256=r.sha256,
                    risk=classify_removal(r), ci=ci,
                    # A DESTROY IS DELIVERED BY THE SAME PUSH. `removed` is
                    # documented as meeting the same bar as `applied` — the
                    # object is gone in SCM AND the push that delivered the
                    # deletion succeeded — so the tombstone has to carry the
                    # push that makes that true, or it asserts it on nothing.
                    push=pushes.get(handler.scope_of(gone).dirname),
                    removal=RemovalContext(ticket=trailers[r.req_id],
                                           commit=ci.merge_commit),
                )
            except EvidenceError as e:
                print(f"error: could not build removal evidence for {r.req_id}: {e}",
                      file=err)
                return 1
            path, is_new = write_bundle_if_changed(bundle, out_root)
            (removed if is_new else unchanged).append(path)

    # UNCHANGED is reported, not silent. The point of leaving a record alone is
    # that its git history stays a history of CHANGES to that request; a run that
    # says nothing about the files it deliberately did not touch looks like a run
    # that lost them.
    print(f"{len(written)} bundle(s) written, {len(removed)} tombstoned, "
          f"{len(unchanged)} unchanged, in {out_root}:", file=out)
    for p in written:
        print(f"  + {p}", file=out)
    for p in removed:
        print(f"  x {p}  (removed — tombstone over the object's own record)", file=out)
    for p in unchanged:
        print(f"  = {p}  (unchanged — record kept from the apply that made it)", file=out)
    if baseline_root is None:
        # SAY SO. A run with no baseline cannot see removals, and reporting only
        # what it did see is how "no removals" and "did not look" become
        # indistinguishable — the failure mode this whole area keeps producing.
        print("note: no --baseline, so REMOVALS were not examined and produced no "
              "record. The apply workflow passes one; a local run must too.", file=out)
    return 0


def _removes_trailers(change_message_path: Optional[Path], err):
    """`req_id -> ticket` from the change message, or None on an IO error.

    An ABSENT path yields an empty mapping, not an error — the caller then
    rejects any removal for want of a trailer, which is the fail-closed outcome.
    An UNREADABLE path is an error: that is a broken pipeline, not an unauthorised
    change, and the two deserve different messages.
    """
    if change_message_path is None:
        return {}
    try:
        return parse_removes_trailers(change_message_path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"error: could not read change message {change_message_path}: {e}", file=err)
        return None


def run_tags(
    action: str,
    scope_dir: str,
    intent_root: Path = Path("intent"),
    env_map_path: Path = Path("catalog/environments.yaml"),
    *,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    dry_run: bool = False,
    session=None,
    out=None,
    err=None,
) -> int:
    """Create or sweep this platform's tag objects (ADR-0009).

    Terraform CREATES nothing and DESTROYS nothing here: changing a tag value on
    a live rule made Terraform run the destroy before the update that released
    it, and SCM refused with 409 NON_ZERO_REFS (measured,
    `spike/tag-destroy-ordering`). So the halves are separated in time — this
    command is both of them.

    `ensure` runs BEFORE apply: the API validates tags as references and rejects
    free-form strings, so a rule cannot be applied before its tags exist.
    `sweep` runs AFTER push, and only ever removes a `gitops:` tag that nothing
    references.

    Exit codes:  0 ok · 1 usage/IO/auth · 2 invalid intent.
    """
    from fwgitops.clients import ScmPushClient  # noqa: F401  (auth stack)
    from fwgitops.compiler import Scope
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession
    from fwgitops.tagsweep import ensure_tags, sweep_tags

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code

    scope = Scope.from_dirname(scope_dir)
    # Every tag any object or rule IN THIS SCOPE carries. Derived from the
    # compiled desired state, so it is exactly what the next apply will
    # reference — which is why a tag in this set is never swept even when
    # nothing references it yet.
    wanted = set()
    for _p, _req, ch in items:
        handler = handler_for_request(_req)
        if handler.scope_of(ch).key != scope.key:
            continue
        for obj in (list(getattr(ch, "address_objects", []))
                    + list(getattr(ch, "service_objects", []))
                    + ([ch.rule] if hasattr(ch, "rule") else [])):
            wanted.update(getattr(obj, "tags", []) or [])

    try:
        session = session or ScmSession(ScmCredentials.from_env())
        params = {"folder": scope.value} if scope.kind == "folder" else {"device": scope.value}
        fn = ensure_tags if action == "ensure" else sweep_tags
        plan = fn(session, params, sorted(wanted), dry_run=dry_run)
    except ScmConfigError as e:
        print(f"error: {e}", file=err)
        return 1
    except ScmApiError as e:
        print(f"error: SCM API: {e}", file=err)
        return 1

    verb = "would " if dry_run else ""
    made, gone = ("create", "remove") if dry_run else ("created", "removed")
    if action == "ensure":
        print(f"OK — {verb}{made} {len(plan.missing)} tag(s) in {scope}; "
              f"{len(plan.referenced)} already referenced, {plan.foreign} not ours",
              file=out)
        for n in plan.missing:
            print(f"  + {n}", file=out)
    else:
        print(f"OK — {verb}{gone} {len(plan.unreferenced)} unreferenced tag(s) in "
              f"{scope}; {len(plan.referenced)} still referenced, {plan.foreign} not ours",
              file=out)
        for n in plan.unreferenced:
            print(f"  - {n}", file=out)
    return 0


def run_objects(
    action: str,
    scope_dir: str,
    intent_root: Path = Path("intent"),
    env_map_path: Path = Path("catalog/environments.yaml"),
    *,
    service_catalog_path: Path = Path("catalog/services.yaml"),
    app_catalog_path: Path = Path("catalog/apps.yaml"),
    dry_run: bool = False,
    session=None,
    out=None,
    err=None,
) -> int:
    """Create or sweep this platform's address and service objects (ADR-0010).

    The same split as `tags`, for the same measured reason one object class
    along. Widening a live rule's destination made Terraform run the address
    DESTROY before the rule UPDATE that released it, and SCM refused with 409
    NON_ZERO_REFS.

    `ensure` runs BEFORE apply and is LOAD-BEARING, not a convenience: the API
    validates these as references and rejects names that do not resolve, so a
    rule cannot be applied before the objects it names exist.

    `sweep` runs AFTER push and removes only objects this platform can PROVE it
    minted — the name must equal the name its own value hashes to — and only
    when nothing references them.

    Exit codes:  0 ok · 1 usage/IO/auth · 2 invalid intent.
    """
    from fwgitops.clients import ScmPushClient  # noqa: F401  (auth stack)
    from fwgitops.compiler import Scope, wanted_objects
    from fwgitops.objectsweep import ensure_objects, sweep_objects
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    cats, ok = _load_catalogs(service_catalog_path, app_catalog_path, err)
    if not ok:
        return 1
    items, code = _compile_intents(intent_root, env_map_path, cats, err)
    if items is None:
        return code

    scope = Scope.from_dirname(scope_dir)
    # Only the changes that land in THIS scope, so a sweep of one folder is
    # never told to protect another folder's objects — or worse, left ignorant
    # of them and free to delete.
    mine = [ch for _p, _req, ch in items
            if handler_for_request(_req).scope_of(ch).key == scope.key
            and hasattr(ch, "rule")]
    wanted = wanted_objects(mine)

    try:
        session = session or ScmSession(ScmCredentials.from_env())
        params = {"folder": scope.value} if scope.kind == "folder" else {"device": scope.value}
        plans = []
        for kind in ("address", "service"):
            if action == "ensure":
                body = {name: _object_body(kind, spec)
                        for name, spec in wanted[kind].items()}
                plans.append(ensure_objects(session, kind, params, body, dry_run=dry_run))
            else:
                plans.append(sweep_objects(session, kind, params,
                                           sorted(wanted[kind]), dry_run=dry_run))
    except ScmConfigError as e:
        print(f"error: {e}", file=err)
        return 1
    except ScmApiError as e:
        print(f"error: SCM API: {e}", file=err)
        return 1

    verb = "would " if dry_run else ""
    made, gone = ("create", "remove") if dry_run else ("created", "removed")
    for plan in plans:
        if action == "ensure":
            print(f"OK — {verb}{made} {len(plan.missing)} {plan.kind} object(s) in "
                  f"{scope}; {len(plan.referenced)} already referenced, "
                  f"{plan.foreign} not ours", file=out)
            for n in plan.missing:
                print(f"  + {n}", file=out)
        else:
            print(f"OK — {verb}{gone} {len(plan.unreferenced)} unreferenced "
                  f"{plan.kind} object(s) in {scope}; {len(plan.referenced)} still "
                  f"referenced, {plan.foreign} not ours", file=out)
            for n in plan.unreferenced:
                print(f"  - {n}", file=out)
    return 0


def _object_body(kind: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """The create body for an object, from the compiler's own dict.

    Built from the compiled spec rather than re-derived, so the value that
    NAMES the object is the value the object is created with. If those two ever
    disagreed the sweep could not prove ownership and would never remove it.
    """
    if kind == "address":
        field = {"ip-netmask": "ip_netmask", "fqdn": "fqdn"}[spec["type"]]
        return {field: spec["value"], "tag": spec.get("tags", [])}
    return {
        "protocol": {spec["protocol"]: {"port": spec["port"]}},
        "tag": spec.get("tags", []),
    }


def run_push(
    folder: Optional[str] = None,
    *,
    device: Optional[str] = None,
    admins: Optional[List[str]] = None,
    all_admins: bool = False,
    record: Optional[Path] = None,
    catalog_path: Path = Path("catalog/folders.yaml"),
    session=None,
    out=None,
    err=None,
) -> int:
    """Push a folder's staged config to SCM (T13). Returns a process exit code.

    A FOLDER WITH NO FIREWALL BENEATH IT IS SKIPPED, not attempted. A push
    commits to devices, so a subtree containing none has nothing to push to and
    SCM rejects the command outright:

        400 Invalid Command
        push-config -> push-to unexpected node here

    Measured 2026-08-15, the first time this pipeline applied a second folder.
    The rule was created and enriched correctly; only the push failed, and it
    failed for a reason the platform already knew — `compile` and
    `verify-catalog` both warn that the folder has no firewall beneath it. The
    check is transitive, because config inherits DOWN: `ngfw-shared` owns no
    device but reaches `prod-edge`'s, so it is pushable.

    Exit codes:  0 ok/noop/skipped · 1 config/auth · 3 push failed.
    Credentials come from SCM_* env; the scm session does its own OAuth. The push
    is ADMIN-SCOPED — it commits only `admins`' staged changes (default: the
    service-account identity), so a shared-candidate folder with out-of-band edits
    is safe by construction. `--all-admins` is the break-glass (whole candidate).
    `session` is injectable for testing.
    """
    # Imported lazily so `fwgitops compile` never needs the SCM stack.
    from fwgitops.catalog import FolderHierarchy
    from fwgitops.clients import ScmPushClient
    from fwgitops.push import PushError, push_folder
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    # NOOP, NOT AN ERROR. Nothing is staged that could ever land, so failing the
    # run would turn a correctly-applied folder into a red apply.
    if folder and catalog_path.is_file():
        try:
            hierarchy = FolderHierarchy.from_dict(read_yaml(catalog_path))
        except Exception:  # noqa: BLE001  — a bad catalog is verify-catalog's job
            hierarchy = None
        if hierarchy is not None and hierarchy.known(folder) \
                and not hierarchy.devices_beneath(folder):
            print(f"skipping push for {folder!r} — no firewall inherits from it, "
                  f"so there is nothing to push to. The config is applied in SCM "
                  f"and will reach a device when one registers.", file=out)
            # RECORD THE SKIP. Writing nothing would leave `push: null`, which
            # already means "nothing was staged" — and a bundle that cannot
            # distinguish a deliberate skip from a failed push is the ambiguity
            # this field exists to remove.
            _write_push_record(record, {
                "folder": folder,
                "status": "skipped",
                "reason": "no_devices_beneath",
            }, err)
            return 0
    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1

    scope = admins or [session.credentials.client_id]
    client = ScmPushClient(session)
    try:
        result = push_folder(client, folder, device=device, admins=scope,
                             all_admins=all_admins)
    except (PushError, ScmApiError) as e:
        print(f"PUSH FAILED: {e}", file=err)
        return 3

    label = "device" if device else "folder"
    print(f"OK — {result.status} ({label}={result.folder} job={result.job_id})", file=out)
    payload = result.to_evidence()
    print(json.dumps(payload, sort_keys=True), file=out)

    # `--record` EXISTS SO THE EVIDENCE BUNDLE CAN NAME THE PUSH. Every bundle
    # ever written carried `"push": null` — the field, the `PushResult` and its
    # `to_evidence()` all existed, and nothing passed one. So the record proved
    # the change was applied to Terraform state and said NOTHING about whether it
    # reached SCM, which is the step that can silently not happen: `devicesync`
    # documents applied-but-unpushed as a real state, and a run once left the
    # ICMP rule uncommitted at version 77 while its bundle read `applied`.
    #
    # SCOPE, NOT FOLDER, is the key. `result.folder` is the SCM address —
    # a bare serial for a device — while evidence groups by Terraform root
    # directory (`device-<serial>`). Writing the address here would silently
    # match nothing for every device-scoped change.
    if record is not None:
        scope = Scope("device", device) if device else Scope("folder", folder)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"scope_dir": scope.dirname, **payload},
                                     sort_keys=True, indent=2) + "\n")
        print(f"push record: {record}", file=out)
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
               if isinstance(ch, REGISTRY["AccessRequest"].compiled_type)
               and ch.rule.folder == folder]
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


def run_adopt_device(
    serial: str,
    *,
    folder: str,
    replacing: Optional[str] = None,
    ticket: Optional[str] = None,
    check: bool = False,
    prune_state: bool = False,
    intent_root: Path = Path("intent"),
    catalog_dir: Path = Path("catalog"),
    session=None,
    out=None,
    err=None,
) -> int:
    """Point the repository at a firewall, reading SCM for every value.

    Adopting a firewall was seventeen hand edits across two catalogs, three
    intents and a Terraform root — each one transcribing something SCM already
    knew. This reads them instead.

    Exit codes:  0 ok · 1 config/auth/IO · 3 SCM refused the adoption.

    `--check` prints the same plan and writes nothing; it is the same code path
    minus the write, not a second implementation.
    """
    from fwgitops.adopt import AdoptError, apply_adoption, plan_adoption
    from fwgitops.clients import ScmDeviceClient
    from fwgitops.scmapi import ScmApiError, ScmConfigError, ScmCredentials, ScmSession

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    ifaces_path = catalog_dir / "interfaces.yaml"
    folders_path = catalog_dir / "folders.yaml"
    for p in (ifaces_path, folders_path):
        if not p.is_file():
            print(f"error: {p} not found", file=err)
            return 1

    # The roles this platform uses are the repository's to declare; the PORT each
    # resolves to is SCM's to say. That split is the whole design.
    try:
        from fwgitops.catalog import InterfaceCatalog
        catalog = InterfaceCatalog.from_dict(read_yaml(ifaces_path))
        roles = {role: catalog.resolve(role, device=None) for role in catalog.roles()}
    except Exception as e:  # noqa: BLE001
        print(f"error: cannot read interface roles from {ifaces_path}: {e}", file=err)
        return 1

    if session is None:
        try:
            session = ScmSession(ScmCredentials.from_env())
        except ScmConfigError as e:
            print(f"error: {e}", file=err)
            return 1

    try:
        adoption = plan_adoption(ScmDeviceClient(session), serial,
                                 folder=folder, roles=roles)
    except AdoptError as e:
        print(f"ADOPTION REFUSED: {e}", file=err)
        return 3
    except ScmApiError as e:
        print(f"error: SCM read failed: {e}", file=err)
        return 1

    print(f"SCM says: {serial} in {adoption.folder!r}"
          + (f", display name {adoption.display_name!r}" if adoption.display_name else ""),
          file=out)
    for role, port in sorted(adoption.ports.items()):
        print(f"  {role:10} -> {port}", file=out)
    for role in adoption.unresolved:
        # NOT an error: a DMZ port is a property of one site's wiring, and a
        # firewall without one is normal. Reported so a partial adoption is
        # visible rather than mistaken for a complete one.
        print(f"  {role:10} -> (no variable in SCM — role left unmapped)", file=out)

    intent_files = {_display_path(p): p.read_text()
                    for p in discover_intents(intent_root)}
    changes = apply_adoption(adoption, folders_text=folders_path.read_text(),
                             interfaces_text=ifaces_path.read_text(),
                             intent_files=intent_files, replacing=replacing,
                             ticket=ticket)

    # The supporting files: fixtures and prose that do not change behaviour but
    # DO break CI. Skipped when nothing is being replaced — there is no old
    # serial to follow.
    if replacing and replacing != serial:
        from fwgitops.adopt import FOLLOW_DIRS, follow_serial
        supporting = {}
        for d in FOLLOW_DIRS:
            for f in sorted(Path(d).rglob("*")):
                if f.is_file() and f.suffix in (".py", ".md", ".yaml", ".yml"):
                    try:
                        supporting[str(f)] = f.read_text()
                    except (OSError, UnicodeDecodeError):
                        continue
        changes.update(follow_serial(replacing, serial, supporting))
    # The Terraform roots are independent of whether the CATALOG changed: a
    # device can be correctly declared and still have no root, which was the
    # early-return bug in the first version of this command.
    new_root = Path("terraform") / f"device-{serial}"
    old_root = (Path("terraform") / f"device-{replacing}"
                if replacing and replacing != serial else None)
    root_work = (not new_root.exists()) or (old_root is not None and old_root.is_dir())

    if not changes and not root_work:
        print("nothing to change — the repository already matches SCM", file=out)
        return 0

    verb = "would write" if check else "wrote"
    if changes:
        print(f"{verb} {len(changes)} file(s):", file=out)
        for rel in sorted(changes):
            print(f"  - {rel}", file=out)
    if check:
        if not new_root.exists():
            print(f"would scaffold {new_root}", file=out)
        if old_root is not None and old_root.is_dir():
            print(f"would remove {old_root}", file=out)
        return 0
    for rel, text in changes.items():
        Path(rel).write_text(text)

    # ── the Terraform roots ───────────────────────────────────────────────
    # `scaffold-root` refuses an existing root on purpose — main.tf is written
    # once — so this asks first rather than treating its error as failure.
    if not new_root.exists():
        rc = run_scaffold_root(Path("terraform"), device=serial,
                               device_folder=folder, out=out, err=err)
        if rc != 0:
            print("::warning::scaffold-root failed; the new device root is missing",
                  file=err)
            return rc

    if old_root is not None and old_root.is_dir():
        import shutil
        shutil.rmtree(old_root)
        print(f"removed {old_root} (including the gitignored files `git rm` "
              f"leaves behind)", file=out)

    print("", file=out)
    if replacing and replacing != serial and not prune_state:
        # THE ONE THING LEFT, AND DELIBERATELY. Deleting Terraform state is
        # irreversible and remote — the difference between "this command edits my
        # repository" and "this command reaches into my cloud account and
        # destroys a record". Opt in with --prune-state.
        print(f"STILL YOURS — the old state object is irreversible, so it is not "
              f"deleted for you:", file=out)
        print(f"  aws s3 rm s3://<state-bucket>/device-{replacing}/terraform.tfstate",
              file=out)
        print(f"  (or re-run with --prune-state)", file=out)
        print("", file=out)
    if replacing and replacing != serial and prune_state:
        key = f"device-{replacing}/terraform.tfstate"
        bucket = _state_bucket(err)
        if not bucket:
            print(f"::warning::--prune-state: could not read the state bucket from "
                  f"terraform/*/backend.hcl; delete {key} by hand", file=err)
        else:
            import subprocess
            uri = f"s3://{bucket}/{key}"
            r = subprocess.run(["aws", "s3", "rm", uri],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"pruned {uri}", file=out)
            else:
                # NOT fatal. The repository is already correct; a state object
                # left behind is inert, and failing here would make the operator
                # re-run an adoption that has nothing left to do.
                print(f"::warning::--prune-state: {r.stderr.strip() or 'failed'} "
                      f"— delete {uri} by hand", file=err)

    print("VERIFY:", file=out)
    print("  fwgitops verify-catalog && fwgitops compile intent --check", file=out)
    return 0


def _state_bucket(err) -> Optional[str]:
    """The state bucket, read from any root's `backend.hcl`.

    Gitignored and generated by `make-backend.sh`, so it is the only place the
    bucket is written down locally. Returns None rather than guessing — deleting
    from the wrong bucket is not a mistake worth risking to save a flag.
    """
    import re as _re
    for f in sorted(Path("terraform").glob("*/backend.hcl")):
        m = _re.search(r'^\s*bucket\s*=\s*"([^"]+)"', f.read_text(), _re.M)
        if m:
            return m.group(1)
    return None


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

    wh = sub.add_parser(
        "where",
        help="which intent authorised this address, name or ticket? (incident response)")
    wh.add_argument("query", help="an IP (10.20.9.10), a CIDR, a zone/app/interface "
                                  "name, a request id, a ticket, or a requester")
    wh.add_argument("intent_root", nargs="?", default="intent", type=Path,
                    help="directory of intent YAML (default: intent)")
    wh.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    wh.add_argument("--evidence", dest="evidence_root", default=Path("evidence"), type=Path,
                    help="where evidence bundles live, so each hit can name its audit "
                         "record (default: evidence)")
    wh.add_argument("--json", dest="as_json", action="store_true",
                    help="machine-readable, for piping into an incident timeline")
    # The catalogs are needed because an intent may name an app whose addresses
    # live there — the raw YAML never contains the CIDR, so a search that skipped
    # them would silently miss every app-based rule.
    wh.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    wh.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)

    fi = sub.add_parser("from-issue",
                        help="turn a filled Issue Form into an intent file (intake)")
    fi.add_argument("--body-file", required=True, type=Path,
                    help="file holding the issue body as GitHub rendered it")
    fi.add_argument("--issue-number", required=True, type=int,
                    help="becomes the request id: REQ-<year>-<number>. Unique by "
                         "construction, and traces the rule back to the conversation.")
    fi.add_argument("--author", required=True,
                    help="the issue author — becomes metadata.requester. NOT a form "
                         "field: one someone types is one they can type wrongly.")
    fi.add_argument("--out", dest="out_root", default=Path("."), type=Path)
    fi.add_argument("--check", action="store_true",
                    help="validate and print the path; write nothing")

    cl = sub.add_parser("classify", help="risk-classify intents (Phase 2, policy-as-code)")
    cl.add_argument("intent_root", nargs="?", default="intent", type=Path,
                    help="directory of intent YAML (default: intent)")
    cl.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path,
                    help="environment resolution map (default: catalog/environments.yaml)")
    cl.add_argument("--state-snapshot", type=Path, action="append", dest="state_snapshots",
                    help="live snapshot from `fwgitops snapshot <kind> <folder>`; repeatable. "
                         "Enables state-aware checks (a zone gaining its first interface, an "
                         "interface gaining addressing). Absent = those checks are skipped.")
    cl.add_argument("--max-tier", action="store_true",
                    help="print ONLY the highest tier in the changeset (LOW|HIGH|CRITICAL) "
                         "and exit 0. The apply workflow routes on this to pick which "
                         "environment — which approver — a run needs.")
    cl.add_argument("--gate", choices=("LOW", "HIGH", "CRITICAL"),
                    help="fail (exit 3) if any change's tier exceeds this max-auto tier")
    cl.add_argument("--baseline", type=Path,
                    help="intent tree of the BASE revision. Enables REMOVAL classification: "
                         "a deleted intent is absent from the current tree, so without this "
                         "nothing classifies it and the gate never sees it. CI materialises "
                         "it with `git archive`.")
    cl.add_argument("--baseline-catalog", dest="baseline_catalog_dir", default=None,
                    type=Path, metavar="DIR",
                    help="the catalog directory that shipped WITH --baseline. A "
                         "baseline is a past state and was valid under its own "
                         "catalog; judging it by today's makes a replaced firewall "
                         "or a renamed folder unrepresentable. Defaults to the "
                         "current catalog, which is right whenever it has not moved.")
    cl.add_argument("--change-message", dest="change_message", default=None, type=Path,
                    help="file holding the text that will land on main (with squash "
                         "merges, the PR title + body). Read for `Removes: <REQ-id> "
                         "(TICKET)` trailers. A removal needs its OWN change ticket: the "
                         "intent's ticket authorised CREATING the object, and the file "
                         "it lived in is being deleted, so there is nowhere else to put "
                         "it. Rejected here rather than at apply, when the PR author is "
                         "still present.")
    cl.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path,
                    help="service name catalog (Phase 2)")
    cl.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path,
                    help="app name catalog (Phase 2)")

    e = sub.add_parser("evidence", help="write NIST-mapped evidence bundles per change (Phase 2)")
    e.add_argument("intent_root", nargs="?", default="intent", type=Path,
                   help="directory of intent YAML (default: intent)")
    e.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    e.add_argument("--baseline", dest="baseline_root", default=None, type=Path,
                   help="the BASE revision's intent tree (CI materialises it with "
                        "`git archive`). Without it a REMOVAL produces no record at "
                        "all: a deleted intent is absent from the current tree, so "
                        "there is nothing left to build one from.")
    e.add_argument("--baseline-catalog", dest="baseline_catalog_dir", default=None,
                   type=Path, metavar="DIR",
                   help="the catalog directory that shipped WITH --baseline. A "
                        "baseline is a past state and was valid under its own "
                        "catalog; judging it by today's makes a replaced firewall "
                        "or a renamed folder unrepresentable. Defaults to the "
                        "current catalog, which is right whenever it has not moved.")
    e.add_argument("--change-message", dest="change_message", default=None, type=Path,
                   help="file holding the text that lands on main (with squash merges, "
                        "the PR title + body). Read for `Removes: <REQ-id> (TICKET)` "
                        "trailers — a removal needs its OWN change ticket, because the "
                        "intent's own ticket authorised CREATING the object and the "
                        "file it lived in is gone.")
    e.add_argument("--approver", dest="approvers", action="append", default=None,
                   metavar="LOGIN[:VIA]",
                   help="who approved this change, repeatable. VIA is "
                        "`pull_request_review` or `deployment_gate`; omitted, the "
                        "approval is recorded as unspecified rather than guessed. "
                        "WITHOUT AT LEAST ONE, the bundle does NOT claim NIST CM-5 — "
                        "that control is about who approved, and an empty list is "
                        "not an answer.")
    e.add_argument("--push-record", dest="push_records", action="append", default=None,
                   type=Path, metavar="FILE",
                   help="a record written by `fwgitops push --record`, repeatable "
                        "(one per scope). WITHOUT IT the bundle says nothing about "
                        "whether the change reached SCM — it proves only that "
                        "Terraform applied it, and applied-but-unpushed is a real "
                        "state this platform has actually been in.")
    e.add_argument("--pr", dest="pr_url", default=None,
                   help="URL of the pull request this change came from (CI resolves "
                        "it from the merge commit).")
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
    dr.add_argument("--snapshot", type=Path,
                    help="JSON/YAML list of the folder's actual rules {folder, name, tags} "
                         "(tag-based drift)")
    dr.add_argument("--state-snapshot", type=Path, action="append", dest="state_snapshots",
                    help="snapshot from `fwgitops snapshot <kind> <folder>`; repeatable. "
                         "State-based drift, for kinds that cannot carry gitops: tags.")
    dr.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    dr.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)

    kd = sub.add_parser("kinds", help="list registered intent kinds (for scripting CI)")
    kd.add_argument("--state-drift", action="store_true",
                    help="only kinds using state-based drift (they cannot carry tags)")
    kd.add_argument("--order", action="store_true",
                    help="print kinds in APPLY order (ADR-0002's chain) instead of "
                         "alphabetically. The apply pipeline consumes this so the "
                         "sequencing lives in the registry, not in a workflow file.")

    ds = sub.add_parser("device-sync",
                        help="is each firewall running what SCM holds? (read-only)")

    vc = sub.add_parser("verify-catalog",
                        help="verify catalog/folders.yaml against SCM's real hierarchy (read-only)")
    vc.add_argument("--folders", default=Path("catalog/folders.yaml"), type=Path)
    vc.add_argument("--interfaces", default=Path("catalog/interfaces.yaml"), type=Path)

    sr = sub.add_parser("scaffold-root",
                        help="create a Terraform root for a scope, or verify/refresh existing ones")
    sr.add_argument("--out", default=Path("terraform"), type=Path,
                    help="Terraform root directory (default: terraform)")
    sr.add_argument("--folder", help="SCM folder this root is for")
    sr.add_argument("--device", help="firewall serial this root is for")
    sr.add_argument("--device-folder",
                    help="with --device: the CONTAINING folder (tags are folder objects)")
    sr.add_argument("--check", action="store_true",
                    help="report roots whose variables.tf no longer mirrors the module")
    sr.add_argument("--sync", action="store_true",
                    help="regenerate variables.tf for every existing root")

    fi = sub.add_parser("folder-interfaces",
                        help="materialise each folder's $-interface variables from the catalog")
    fi.add_argument("--out", default=Path("terraform"), type=Path,
                    help="Terraform root directory (default: terraform)")
    fi.add_argument("--interfaces", default=Path("catalog/interfaces.yaml"), type=Path,
                    help="interface catalog (default: catalog/interfaces.yaml)")
    fi.add_argument("--folders", default=Path("catalog/folders.yaml"), type=Path,
                    help="folder hierarchy (default: catalog/folders.yaml)")
    fi.add_argument("--check", action="store_true",
                    help="validate and report without writing files")

    ao = sub.add_parser("apply-order",
                        help="print Terraform roots in APPLY order (ADR-0002's ordered chain)")
    ao.add_argument("--out", default=Path("terraform"), type=Path,
                    help="Terraform root directory (default: terraform)")

    sn = sub.add_parser("snapshot",
                        help="read a folder's live objects of one kind from SCM (read-only)")
    sn.add_argument("kind", help="intent kind, e.g. ZoneRequest / InterfaceRequest")
    sn.add_argument("folder", nargs="?", default=None, help="SCM folder to read")
    sn.add_argument("--scope-dir",
                    help="a Terraform root DIRECTORY name (e.g. `prod-edge` or "
                         "`device-<serial>`); resolved to the right scope via "
                         "Scope.from_dirname. Lets a caller iterate terraform/*/ "
                         "without re-implementing the naming convention.")
    sn.add_argument("--device", default=None,
                    help="read a FIREWALL's scope instead of a folder (serial). A firewall "
                         "is the last level of the hierarchy but is addressed device=, "
                         "never folder=.")
    sn.add_argument("--out", required=True, type=Path, help="where to write the snapshot JSON")

    tg = sub.add_parser("tags",
                        help="create or sweep this platform's tag objects (ADR-0009)")
    tg.add_argument("action", choices=("ensure", "sweep"),
                    help="ensure: create missing tags, run BEFORE apply. "
                         "sweep: remove `gitops:` tags nothing references, run AFTER push. "
                         "Terraform does neither — it ran a tag destroy before the rule "
                         "update that released it and 409'd (spike/tag-destroy-ordering).")
    tg.add_argument("scope_dir",
                    help="a Terraform root DIRECTORY name (`prod-edge` or `device-<serial>`)")
    tg.add_argument("intent_root", nargs="?", default=Path("intent"), type=Path)
    tg.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    tg.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    tg.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)
    tg.add_argument("--dry-run", action="store_true", help="report, write nothing")

    ob = sub.add_parser("objects",
                        help="create or sweep this platform's address/service objects "
                             "(ADR-0010)")
    ob.add_argument("action", choices=("ensure", "sweep"),
                    help="ensure: create missing objects, run BEFORE apply — the API "
                         "rejects a rule naming an object that does not exist, so this "
                         "is load-bearing. sweep: remove objects nothing references, run "
                         "AFTER push. Terraform does neither — it ran an address destroy "
                         "before the rule update that released it and 409'd.")
    ob.add_argument("scope_dir",
                    help="a Terraform root DIRECTORY name (`prod-edge` or `device-<serial>`)")
    ob.add_argument("intent_root", nargs="?", default=Path("intent"), type=Path)
    ob.add_argument("--env-map", default=Path("catalog/environments.yaml"), type=Path)
    ob.add_argument("--service-catalog", default=Path("catalog/services.yaml"), type=Path)
    ob.add_argument("--app-catalog", default=Path("catalog/apps.yaml"), type=Path)
    ob.add_argument("--dry-run", action="store_true", help="report, write nothing")

    p = sub.add_parser("push", help="push a folder's or firewall's staged config to SCM (T13)")
    p.add_argument("folder", nargs="?", default=None, help="SCM folder to push")
    p.add_argument("--device", default=None,
                   help="push a FIREWALL instead (serial). A device-scope override belongs "
                        "to the firewall; pushing its folder would commit whatever else is "
                        "staged there.")
    p.add_argument("--scope-dir",
                   help="a Terraform root DIRECTORY name (`prod-edge` or "
                        "`device-<serial>`), resolved to the right scope. The apply loop "
                        "iterates those directories, and stripping the `device-` prefix in "
                        "the workflow instead is the duplication that broke the drift job "
                        "in v1.34.2 — `Scope.from_dirname` owns the mapping.")
    p.add_argument("--admin", action="append", dest="admins",
                   help="identity whose staged changes to commit (repeatable); "
                        "default: SCM_CLIENT_ID. Scopes the commit so out-of-band "
                        "edits are never swept in.")
    p.add_argument("--record", default=None, type=Path, metavar="FILE",
                   help="write the push outcome here as JSON, for "
                        "`fwgitops evidence --push-record`. Keyed by Terraform root "
                        "directory, not by the SCM address, because that is how a "
                        "change knows its own scope.")
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
    ad = sub.add_parser("adopt-device",
                        help="point the repository at a firewall, reading SCM for every value")
    ad.add_argument("serial", help="device serial number (from ssh 'show system info')")
    ad.add_argument("--folder", required=True,
                    help="the SCM folder the device must ALREADY be in. Adoption "
                         "refuses if SCM disagrees — writing the folder you meant "
                         "would make the catalog assert a placement that is not real.")
    ad.add_argument("--replacing", default=None, metavar="OLD_SERIAL",
                    help="the serial this one replaces. Rewrites it across both "
                         "catalogs and every device-scoped intent, which is the "
                         "partial-rename failure this command exists to remove.")
    ad.add_argument("--ticket", default=None, metavar="TICKET",
                    help="the change ticket authorising THIS adoption. A "
                         "replacement changes `spec.device` on every "
                         "device-scoped intent, and a changed spec carrying the "
                         "previous ticket is rejected — so without this the "
                         "command writes a pull request that cannot merge, "
                         "failing a gate its own edit triggered.")
    ad.add_argument("--check", action="store_true",
                    help="print the plan and write nothing — the same code path "
                         "minus the write")
    ad.add_argument("--prune-state", action="store_true",
                    help="also delete the replaced device's Terraform state from "
                         "the backend. OFF by default because it is irreversible "
                         "and REMOTE — the difference between editing your "
                         "repository and reaching into your cloud account. Without "
                         "it the command prints the one-liner.")
    ad.add_argument("--intent-root", default=Path("intent"), type=Path)
    ad.add_argument("--catalog", dest="catalog_dir", default=Path("catalog"), type=Path)

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
    if args.command == "where":
        return run_where(
            args.query, args.intent_root, args.env_map,
            evidence_root=args.evidence_root, as_json=args.as_json,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "from-issue":
        return run_from_issue(args.body_file, args.issue_number, args.author,
                              out_root=args.out_root, write=not args.check)
    if args.command == "classify":
        return run_classify(
            args.intent_root, args.env_map, gate=args.gate,
            state_snapshot_paths=args.state_snapshots,
            baseline_root=args.baseline,
            baseline_catalog_dir=args.baseline_catalog_dir,
            change_message_path=args.change_message,
            max_tier=args.max_tier,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "evidence":
        return run_evidence(
            args.intent_root, args.env_map, args.out, status=args.status,
            tfvars_root=args.tfvars_root,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
            baseline_root=args.baseline_root,
            baseline_catalog_dir=args.baseline_catalog_dir,
            change_message_path=args.change_message,
            approvers=args.approvers, pr_url=args.pr_url,
            push_records=args.push_records,
        )
    if args.command == "drift":
        return run_drift(
            args.intent_root, args.env_map, args.snapshot,
            state_snapshot_paths=args.state_snapshots,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
        )
    if args.command == "kinds":
        if args.order:
            # Apply order is NOT sorted afterwards — sorting it would discard the
            # very thing being asked for.
            try:
                ordered = kind_apply_order()
            except KindOrderError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            if args.state_drift:
                state = {h.kind for h in kinds_with_drift_engine("state")}
                ordered = [k for k in ordered if k in state]
            for name in ordered:
                print(name)
            return 0
        names = ([h.kind for h in kinds_with_drift_engine("state")]
                 if args.state_drift else list(REGISTRY))
        for name in sorted(names):
            print(name)
        return 0
    if args.command == "device-sync":
        return run_device_sync()

    if args.command == "verify-catalog":
        return run_verify_catalog(folders_path=args.folders,
                                  interface_catalog_path=args.interfaces)

    if args.command == "scaffold-root":
        return run_scaffold_root(
            args.out, folder=args.folder, device=args.device,
            device_folder=args.device_folder, check=args.check, sync=args.sync,
        )

    if args.command == "folder-interfaces":
        return run_folder_interfaces(
            args.out,
            interface_catalog_path=args.interfaces,
            folders_path=args.folders,
            write=not args.check,
        )

    if args.command == "apply-order":
        return run_apply_order(args.out)
    if args.command == "snapshot":
        if args.scope_dir:
            # Resolve a Terraform root DIRECTORY to its scope. The drift job
            # iterates terraform/*/ and previously passed each name as a FOLDER,
            # which SCM rejects for the device root ("Folder
            # device-<serial> doesn't exist"). Scope.from_dirname owns the
            # mapping so it cannot drift from Scope.dirname.
            from fwgitops.compiler import Scope
            if args.folder or args.device:
                print("error: --scope-dir replaces <folder> / --device; give one form",
                      file=sys.stderr)
                return 1
            scope = Scope.from_dirname(args.scope_dir)
            return run_snapshot(args.kind, scope.value if scope.kind == "folder" else None,
                                args.out,
                                device=scope.value if scope.kind == "device" else None)
        if bool(args.folder) == bool(args.device):
            print("error: give exactly one of <folder> or --device <serial> — a firewall "
                  "is the last level of the hierarchy but is addressed device=, never "
                  "folder= (which returns 400 'Folder doesn't exist').", file=sys.stderr)
            return 1
        return run_snapshot(args.kind, args.folder, args.out, device=args.device)
    if args.command == "tags":
        return run_tags(
            args.action, args.scope_dir, args.intent_root, args.env_map,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
            dry_run=args.dry_run,
        )
    if args.command == "objects":
        return run_objects(
            args.action, args.scope_dir, args.intent_root, args.env_map,
            service_catalog_path=args.service_catalog, app_catalog_path=args.app_catalog,
            dry_run=args.dry_run,
        )
    if args.command == "push":
        if args.scope_dir:
            from fwgitops.compiler import Scope
            if args.folder or args.device:
                print("error: --scope-dir replaces <folder> / --device; give one form",
                      file=sys.stderr)
                return 1
            scope = Scope.from_dirname(args.scope_dir)
            return run_push(
                scope.value if scope.kind == "folder" else None,
                device=scope.value if scope.kind == "device" else None,
                admins=args.admins, all_admins=args.all_admins,
                record=args.record,
            )
        if bool(args.folder) == bool(args.device):
            print("error: give exactly one of <folder> or --device <serial>. A device-scope "
                  "override belongs to the firewall; pushing its folder would commit "
                  "whatever else is staged there.", file=sys.stderr)
            return 1
        return run_push(
            args.folder,
            device=args.device,
            admins=args.admins,
            all_admins=args.all_admins,
            record=args.record,
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
    if args.command == "adopt-device":
        return run_adopt_device(args.serial, folder=args.folder,
                                replacing=args.replacing, ticket=args.ticket,
                                check=args.check,
                                prune_state=args.prune_state,
                                intent_root=args.intent_root,
                                catalog_dir=args.catalog_dir)
    if args.command == "deregister":
        return run_deregister(args.serial)
    if args.command == "set-admin-password":
        return run_set_admin_password(args.mgmt_ip, ssh_key=args.ssh_key, user=args.user)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
