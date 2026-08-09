"""Tests for the CLI compile command (reads real YAML on disk)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fwgitops.cli import run_compile

VALID_INTENT = """\
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0417
  requester: jane.doe@corp
  ticket: JIRA-12345
  justification: "Web tier needs to reach the payments API"
  requested: 2026-07-19
spec:
  environment: prod
  action: allow
  source:
    - cidr: 10.20.1.0/24
  destination:
    - cidr: 10.20.9.10/32
  service:
    - protocol: tcp
      port: 443
  log: true
"""

ENV_MAP = "prod:\n  folder: prod-edge\n  from_zone: trust\n  to_zone: app\n"


def _setup(tmp_path: Path, intent_body: str = VALID_INTENT, name: str = "REQ.yaml") -> tuple:
    intent_root = tmp_path / "intent" / "prod"
    intent_root.mkdir(parents=True)
    (intent_root / name).write_text(intent_body)
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"
    return tmp_path / "intent", env_map, out


def test_compile_writes_tfvars(tmp_path, capsys):
    intent_root, env_map, out = _setup(tmp_path)
    rc = run_compile(intent_root, env_map, out, require_terraform_root=False)
    assert rc == 0
    target = out / "prod-edge" / "rules.auto.tfvars.json"
    assert target.is_file()
    data = json.loads(target.read_text())
    assert "REQ-2026-0417" in data["security_rules"]
    assert data["security_rules"]["REQ-2026-0417"]["folder"] == "prod-edge"


def test_check_mode_writes_nothing(tmp_path):
    intent_root, env_map, out = _setup(tmp_path)
    rc = run_compile(intent_root, env_map, out, write=False, require_terraform_root=False)
    assert rc == 0
    assert not (out / "prod-edge" / "rules.auto.tfvars.json").exists()


def test_invalid_intent_exits_2_and_writes_nothing(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: permit")
    intent_root, env_map, out = _setup(tmp_path, intent_body=bad)
    rc = run_compile(intent_root, env_map, out, require_terraform_root=False)
    assert rc == 2
    assert not out.exists()  # all-or-nothing: nothing written on failure
    err = capsys.readouterr().err
    assert "REJECTED" in err and "spec.action" in err


def test_unknown_environment_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("environment: prod", "environment: staging")
    intent_root, env_map, out = _setup(tmp_path, intent_body=bad)
    rc = run_compile(intent_root, env_map, out, require_terraform_root=False)
    assert rc == 2
    assert "staging" in capsys.readouterr().err


def test_one_bad_file_blocks_the_whole_run(tmp_path):
    # A valid + an invalid intent: the run rejects, writes nothing (all-or-nothing).
    intent_root, env_map, out = _setup(tmp_path, name="good.yaml")
    (intent_root / "prod" / "bad.yaml").write_text(
        VALID_INTENT.replace("id: REQ-2026-0417", "id: REQ-2026-0500").replace(
            "port: 443", "port: 70000"
        )
    )
    rc = run_compile(intent_root, env_map, out, require_terraform_root=False)
    assert rc == 2
    assert not out.exists()


def test_example_files_are_skipped(tmp_path):
    intent_root, env_map, out = _setup(tmp_path)
    # An .example. file that is intentionally not valid Phase-1 intent.
    (intent_root / "prod" / "REQ.example.yaml").write_text("apiVersion: nope\n")
    rc = run_compile(intent_root, env_map, out, require_terraform_root=False)
    assert rc == 0  # example ignored, valid intent still compiles


def test_compile_rejects_undeclared_zone(tmp_path, capsys):
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    (root / "REQ.yaml").write_text(
        "apiVersion: fw-intent/v1\n"
        "kind: AccessRequest\n"
        "metadata: {id: REQ-Z, requester: m@corp, ticket: J-1, justification: x, requested: 2026-07-27}\n"
        "spec:\n"
        "  environment: prod\n"
        "  action: allow\n"
        "  source: [{app: dmz-app}]\n"
        "  destination: [{cidr: 10.20.9.10/32}]\n"
        "  service: [{protocol: tcp, port: \"443\"}]\n"
        "  log: true\n"
    )
    env_map = tmp_path / "env.yaml"
    env_map.write_text(ENV_MAP)  # prod -> prod-edge, zones trust/app
    apps = tmp_path / "apps.yaml"
    apps.write_text(
        "apps:\n  dmz-app: {environment: prod, zone: dmz, addresses: [10.20.1.0/24]}\n"
    )
    out = tmp_path / "tf"
    rc = run_compile(tmp_path / "intent", env_map, out, app_catalog_path=apps, require_terraform_root=False)
    assert rc == 2
    err = capsys.readouterr().err
    assert "dmz" in err and "ZoneRequest" in err
    assert not (out / "prod-edge" / "rules.auto.tfvars.json").exists()  # nothing written


def test_compile_zone_request_writes_zones_tfvars(tmp_path, capsys):
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    (root / "ZONE.yaml").write_text(
        "apiVersion: fw-intent/v1\n"
        "kind: ZoneRequest\n"
        "metadata: {id: ZONE-1, requester: m@corp, ticket: J-1, justification: dmz, requested: 2026-07-27}\n"
        "spec: {environment: prod, zone: dmz, type: layer3, interfaces: [ethernet1/2]}\n"
    )
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"
    rc = run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False)
    assert rc == 0
    zf = json.loads((out / "prod-edge" / "zones.auto.tfvars.json").read_text())
    assert zf["zones"]["dmz"]["network"]["layer3"] == ["ethernet1/2"]
    assert zf["zones"]["dmz"]["folder"] == "prod-edge"
    # a ZoneRequest-only compile writes no rules file
    assert not (out / "prod-edge" / "rules.auto.tfvars.json").exists()


def test_no_intents_is_ok(tmp_path, capsys):
    (tmp_path / "intent").mkdir()
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    rc = run_compile(tmp_path / "intent", env_map, tmp_path / "terraform", require_terraform_root=False)
    assert rc == 0
    assert "no intent files" in capsys.readouterr().out


def test_missing_env_map_exits_1(tmp_path, capsys):
    (tmp_path / "intent").mkdir()
    rc = run_compile(tmp_path / "intent", tmp_path / "nope.yaml", tmp_path / "terraform", require_terraform_root=False)
    assert rc == 1
    assert "env map not found" in capsys.readouterr().err


# ── fwgitops classify (Phase 2) ────────────────────────────────────────────
from fwgitops.cli import run_classify  # noqa: E402


def test_classify_reports_tier(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    rc = run_classify(intent_root, env_map)  # report-only -> exit 0 whatever the tier
    assert rc == 0
    o = capsys.readouterr().out
    assert "REQ-2026-0417" in o and "classified 1" in o


def _second_intent_same_zone_pair(intent_root):
    # A second prod intent (same trust->app zone-pair, different source) so
    # neither rule is a "novel zone-pair" -> both classify on stateless merits.
    body = VALID_INTENT.replace("REQ-2026-0417", "REQ-2026-0999").replace(
        "10.20.1.0/24", "10.20.5.0/24")
    (intent_root / "prod" / "REQ2.yaml").write_text(body)


def test_classify_rejects_invalid_intent_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: permit")
    intent_root, env_map, _ = _setup(tmp_path, intent_body=bad)
    rc = run_classify(intent_root, env_map)
    assert rc == 2


_BROAD = VALID_INTENT.replace("cidr: 10.20.1.0/24", "cidr: 10.0.0.0/8")  # broad_source -> HIGH


def test_classify_gate_blocks_change_above_tier(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path, intent_body=_BROAD)
    rc = run_classify(intent_root, env_map, gate="LOW")
    assert rc == 3
    assert "GATE" in capsys.readouterr().err


def test_classify_gate_allows_low_change(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    _second_intent_same_zone_pair(intent_root)  # shared zone-pair -> not novel -> LOW
    assert run_classify(intent_root, env_map, gate="LOW") == 0


def test_classify_gate_higher_override_allows_high(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path, intent_body=_BROAD)
    assert run_classify(intent_root, env_map, gate="CRITICAL") == 0  # explicit override


# ── fwgitops evidence (Phase 2) ────────────────────────────────────────────
from fwgitops.cli import run_evidence  # noqa: E402


def test_evidence_writes_bundle_with_risk_and_provenance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    intent_root, env_map, _ = _setup(tmp_path)
    out = tmp_path / "ev"
    rc = run_evidence(intent_root, env_map, out, tfvars_root=tmp_path / "none")
    assert rc == 0
    b = json.loads((out / "prod-edge" / "REQ-2026-0417.json").read_text())
    assert b["schema"] == "fw-evidence/v2" and b["status"] == "applied"
    assert b["kind"] == "AccessRequest"
    assert b["risk"]["classifier_version"]                    # risk verdict recorded
    assert b["apply"]["run_url"].endswith("/actions/runs/42")  # CI provenance
    assert set(b["controls"]) >= {"AC-4", "CM-3", "AU-12"}


def test_evidence_covers_EVERY_intent_in_the_shipped_tree(tmp_path):
    """One bundle per intent file, whatever kind it is.

    THE REGRESSION THIS PINS. `run_evidence` filtered to `AccessRequest`, so on
    2026-08-08 the repo's ten intents produced five bundles — and the command
    printed "wrote 5 evidence bundle(s)" and exited 0, so nothing anywhere said
    the other five had no audit record. A `RouteRequest` decides where every
    unmatched packet goes; changing one left nothing to read afterwards.

    Counting against the TREE rather than a fixed number is the point: adding an
    intent of a kind nobody wired into evidence fails here instead of shipping
    with a silent hole.
    """
    from pathlib import Path

    from fwgitops.io import discover_intents
    from fwgitops.kinds import REGISTRY

    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "ev"
    rc = run_evidence(repo / "intent", repo / "catalog" / "environments.yaml", out,
                      tfvars_root=repo / "terraform",
                      service_catalog_path=repo / "catalog" / "services.yaml",
                      app_catalog_path=repo / "catalog" / "apps.yaml")
    assert rc == 0
    bundles = sorted(out.rglob("*.json"))
    intents = discover_intents(repo / "intent")
    assert len(bundles) == len(intents), (
        f"{len(intents)} intents produced {len(bundles)} bundles — a change with "
        f"no evidence is a change with no audit record")

    kinds = {json.loads(p.read_text())["kind"] for p in bundles}
    assert kinds == set(REGISTRY), f"no bundle for kind(s) {set(REGISTRY) - kinds}"
    for p in bundles:
        b = json.loads(p.read_text())
        assert b["compiled"]["object"], f"{p.name}: empty compiled object"
        assert b["risk"]["tier"] != "not_classified", f"{p.name}: unclassified"


def test_evidence_rejects_invalid_intent_exits_2(tmp_path, capsys):
    bad = VALID_INTENT.replace("action: allow", "action: nope")
    intent_root, env_map, _ = _setup(tmp_path, intent_body=bad)
    assert run_evidence(intent_root, env_map, tmp_path / "ev") == 2


# ── fwgitops drift (Phase 2) ───────────────────────────────────────────────
from fwgitops.cli import run_drift  # noqa: E402

_MANAGED_417 = ["gitops:managed", "gitops:req:REQ-2026-0417"]


def test_drift_flags_unmanaged_exit_3(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)  # declares REQ-2026-0417 in prod-edge
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps([
        {"folder": "prod-edge", "name": "REQ-2026-0417", "tags": _MANAGED_417},
        {"folder": "prod-edge", "name": "MANUAL-RULE", "tags": []},
    ]))
    rc = run_drift(intent_root, env_map, snap)
    assert rc == 3
    assert "MANUAL-RULE" in capsys.readouterr().out


def test_drift_clean_exit_0(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps([{"folder": "prod-edge", "name": "REQ-2026-0417", "tags": _MANAGED_417}]))
    assert run_drift(intent_root, env_map, snap) == 0


def test_drift_missing_snapshot_exit_1(tmp_path, capsys):
    intent_root, env_map, _ = _setup(tmp_path)
    assert run_drift(intent_root, env_map, tmp_path / "nope.json") == 1


# ── fwgitops push (T13) ────────────────────────────────────────────────────
from fwgitops.cli import run_push  # noqa: E402
from fwgitops.scmapi import ScmCredentials, ScmSession  # noqa: E402

SA = "GitOps@1198884949.iam.panserviceaccount.com"
CREDS = ScmCredentials(client_id=SA, client_secret="s3cret", scope="tsg_id:1198884949")


def _scm_transport(nothing_to_push=False, sink=None):
    def t(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        if "candidate:push" in url:                         # POST push
            if sink is not None:
                sink.append(json.loads(body))
            if nothing_to_push:
                return 400, json.dumps({"message": "no changes to push"}).encode()
            return 200, json.dumps({"job_id": "job-9"}).encode()
        if "/jobs/" in url:                                 # GET job status
            return 200, json.dumps({"data": [{"status_str": "FIN", "result_str": "OK"}]}).encode()
        return 404, b"{}"
    return t


def _session(**kw):
    return ScmSession(CREDS, transport=_scm_transport(**kw))


def test_cli_push_success_scoped_to_service_account(capsys):
    sink = []
    rc = run_push("GitOps", session=ScmSession(CREDS, transport=_scm_transport(sink=sink)))
    assert rc == 0
    assert "OK — success" in capsys.readouterr().out
    assert sink[-1]["admin"] == [SA]          # default: commit only our SA's changes


def test_cli_push_all_admins_is_unscoped(capsys):
    sink = []
    rc = run_push("GitOps", all_admins=True,
                  session=ScmSession(CREDS, transport=_scm_transport(sink=sink)))
    assert rc == 0
    assert "admin" not in sink[-1]            # break-glass: whole candidate


def test_cli_push_noop_when_nothing_staged(capsys):
    rc = run_push("GitOps", session=_session(nothing_to_push=True))
    assert rc == 0
    assert "noop" in capsys.readouterr().out


def test_cli_push_missing_env_exits_1(monkeypatch, capsys):
    for v in ("SCM_CLIENT_ID", "SCM_CLIENT_SECRET", "SCM_SCOPE"):
        monkeypatch.delenv(v, raising=False)
    rc = run_push("GitOps")   # no session -> from_env -> missing -> 1
    assert rc == 1


from fwgitops.cli import run_set_admin_password  # noqa: E402


def test_cli_set_admin_password_missing_phash_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("FWGITOPS_ADMIN_PHASH", raising=False)
    rc = run_set_admin_password("10.0.0.1", ssh_key="k.pem")  # no phash -> 1, no SSH
    assert rc == 1
    assert "FWGITOPS_ADMIN_PHASH" in capsys.readouterr().err


# ── fwgitops rules (live SCM read — the "is my rule deployed?" check) ───────
from fwgitops.cli import run_rules  # noqa: E402


def _rules_transport(names):
    def t(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        if "/security-rules" in url:
            return 200, json.dumps({"data": [{"name": n, "id": f"id-{n}"} for n in names]}).encode()
        return 404, b"{}"
    return t


def _rules_session(names):
    return ScmSession(CREDS, transport=_rules_transport(names))


def test_cli_rules_lists_live(capsys):
    rc = run_rules("prod-edge", session=_rules_session(["REQ-B", "REQ-A"]))
    out = capsys.readouterr().out
    assert rc == 0 and "REQ-A" in out and "REQ-B" in out


def test_cli_rules_has_present(capsys):
    rc = run_rules("prod-edge", contains="REQ-A", session=_rules_session(["REQ-A", "REQ-B"]))
    assert rc == 0 and "LIVE" in capsys.readouterr().out


def test_cli_rules_has_absent(capsys):
    rc = run_rules("prod-edge", contains="REQ-Z", session=_rules_session(["REQ-A"]))
    assert rc == 3 and "NOT FOUND" in capsys.readouterr().out


def test_compile_rejects_a_zone_request_colliding_with_a_baseline_zone(tmp_path, capsys):
    """End-to-end guard for check_zone_collisions: without this, removing the
    call site from run_compile leaves the whole suite green."""
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    (root / "ZONE.yaml").write_text(
        "apiVersion: fw-intent/v1\n"
        "kind: ZoneRequest\n"
        "metadata: {id: ZONE-1, requester: m@corp, ticket: J-1, justification: dup,"
        " requested: 2026-07-27}\n"
        "spec: {environment: prod, zone: proxy, type: layer3, interfaces: []}\n"
    )
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP + "  baseline_zones: [proxy]\n")
    out = tmp_path / "terraform"
    rc = run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False)
    assert rc == 2
    assert "already" in capsys.readouterr().err
    assert not (out / "prod-edge" / "zones.auto.tfvars.json").exists()


def test_duplicate_zone_key_reports_and_exits_2_not_a_traceback(tmp_path, capsys):
    """CompileError from the planning loop used to escape as a raw traceback and
    exit 1. Every other compile-stage rejection reports and returns 2."""
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    for n in (1, 2):
        (root / f"Z{n}.yaml").write_text(
            "apiVersion: fw-intent/v1\nkind: ZoneRequest\n"
            f"metadata: {{id: ZONE-{n}, requester: m@corp, ticket: J-{n},"
            " justification: dup, requested: 2026-07-27}\n"
            "spec: {environment: prod, zone: dmz, type: layer3, interfaces: []}\n"
        )
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    rc = run_compile(tmp_path / "intent", env_map, tmp_path / "terraform",
                     require_terraform_root=False)
    assert rc == 2
    err = capsys.readouterr().err
    assert "REJECTED" in err and "duplicate zone key" in err


ZONE_SEC_ENV = "prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n"


def _write_zone(root, zid, zone, extra=""):
    (root / f"{zid}.yaml").write_text(
        "apiVersion: fw-intent/v1\nkind: ZoneRequest\n"
        f"metadata: {{id: {zid}, requester: m@corp, ticket: J-1,"
        " justification: x, requested: 2026-08-02}\n"
        f"spec:\n  environment: prod\n  zone: {zone}\n  type: layer3\n  interfaces: []\n{extra}"
    )


def test_classify_covers_zones_and_gates_on_them(tmp_path, capsys):
    """run_classify used to drop zones on the floor ("policy stages: rules only"),
    so a zone with no protection profile was never risk-assessed at all."""
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    _write_zone(root, "Z-BARE", "dmz-bare")
    _write_zone(root, "Z-ARMED", "dmz-armed",
                "  protection_profile: best-practice\n  user_id: true\n")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ZONE_SEC_ENV)

    rc = run_classify(tmp_path / "intent", env_map)
    out = capsys.readouterr().out
    assert rc == 0
    assert "zone/dmz-bare" in out and "zone_without_protection" in out
    assert "zone/dmz-armed" in out

    # An unprotected zone is HIGH, so a LOW gate must refuse to auto-apply it.
    assert run_classify(tmp_path / "intent", env_map, gate="LOW") == 3


def test_a_fully_configured_zone_passes_a_low_gate(tmp_path):
    root = tmp_path / "intent" / "prod"
    root.mkdir(parents=True)
    _write_zone(root, "Z-ARMED", "dmz-armed",
                "  protection_profile: best-practice\n  user_id: true\n")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ZONE_SEC_ENV)
    assert run_classify(tmp_path / "intent", env_map, gate="LOW") == 0


def test_drift_requires_at_least_one_snapshot(tmp_path, capsys):
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    assert run_drift(tmp_path / "intent", env_map) == 1
    assert "--snapshot" in capsys.readouterr().err


def test_drift_reports_a_locally_defined_undeclared_zone(tmp_path, capsys):
    import json
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    snap = tmp_path / "zones.json"
    snap.write_text(json.dumps([
        # inherited from the parent folder -> not this folder's drift
        {"kind": "ZoneRequest", "name": "internet", "folder": "ngfw-shared",
         "scope": "prod-edge"},
        # defined locally, declared nowhere -> unexpected
        {"kind": "ZoneRequest", "name": "rogue", "folder": "prod-edge",
         "scope": "prod-edge"},
    ]))
    rc = run_drift(tmp_path / "intent", env_map, state_snapshot_paths=[snap])
    out = capsys.readouterr().out
    assert rc == 3
    assert "unexpected" in out and "rogue" in out
    assert "inherited" in out and "rogue" not in out.split("inherited")[1]


def test_classify_uses_the_zone_snapshot_scope_not_the_defining_folder(tmp_path, capsys):
    """Subtle and load-bearing: SCM returns the folder an object is DEFINED in.
    The classifier must key on the QUERIED folder, or an inherited zone never
    matches its declaration and the state-aware checks silently never fire."""
    import json
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    (root / "Z.yaml").write_text(
        "apiVersion: fw-intent/v1\nkind: ZoneRequest\n"
        "metadata: {id: Z1, requester: m@corp, ticket: J-1, justification: x,"
        " requested: 2026-08-02}\n"
        "spec:\n  environment: prod\n  zone: zone-internal\n  type: layer3\n"
        "  interfaces: ['$eth-local']\n  protection_profile: best-practice\n  user_id: true\n"
    )
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    snap = tmp_path / "zones.json"
    # defined in the parent, queried at prod-edge — exactly the live shape
    snap.write_text(json.dumps([{"name": "zone-internal", "folder": "ngfw-shared",
                                 "scope": "prod-edge", "network": {"layer3": []}}]))

    rc = run_classify(tmp_path / "intent", env_map, state_snapshot_paths=[snap], gate="LOW")
    out = capsys.readouterr().out
    assert "zone_becomes_traffic_bearing" in out
    assert rc == 3, "a zone starting to carry traffic must not auto-apply at LOW"


def test_drift_rejects_a_snapshot_with_no_kind_stamp(tmp_path, capsys):
    """Drift must not GUESS which kind a snapshot holds — mis-attributing it
    would compare against the wrong declared set entirely."""
    import json
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    snap = tmp_path / "s.json"
    snap.write_text(json.dumps([{"name": "x", "folder": "prod-edge"}]))
    assert run_drift(tmp_path / "intent", env_map, state_snapshot_paths=[snap]) == 1
    assert "no `kind` field" in capsys.readouterr().err


def test_drift_covers_interfaces_not_just_zones(tmp_path, capsys):
    """THE GAP THIS CLOSES. The registry declared drift_engine="state" for
    InterfaceRequest while the drift engine only knew about zones, so an
    interface added by hand was invisible to everything."""
    import json
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    snap = tmp_path / "ifaces.json"
    snap.write_text(json.dumps([
        {"kind": "InterfaceRequest", "name": "$eth-rogue", "folder": "prod-edge",
         "scope": "prod-edge", "layer3": {}},
    ]))
    rc = run_drift(tmp_path / "intent", env_map, state_snapshot_paths=[snap])
    out = capsys.readouterr().out
    assert rc == 3
    assert "InterfaceRequest" in out and "unexpected" in out and "$eth-rogue" in out


def test_drift_handles_several_kinds_in_one_run(tmp_path, capsys):
    import json
    root = tmp_path / "intent" / "prod"; root.mkdir(parents=True)
    env_map = tmp_path / "environments.yaml"; env_map.write_text(ENV_MAP)
    z = tmp_path / "z.json"; i = tmp_path / "i.json"
    z.write_text(json.dumps([{"kind": "ZoneRequest", "name": "rogue-zone",
                              "folder": "prod-edge", "scope": "prod-edge"}]))
    i.write_text(json.dumps([{"kind": "InterfaceRequest", "name": "$eth-rogue",
                              "folder": "prod-edge", "scope": "prod-edge"}]))
    rc = run_drift(tmp_path / "intent", env_map, state_snapshot_paths=[z, i])
    out = capsys.readouterr().out
    assert rc == 3
    assert "rogue-zone" in out and "$eth-rogue" in out


def test_snapshot_requires_exactly_one_scope(capsys):
    """A firewall is the last level of the hierarchy but is addressed `device=`,
    never `folder=` (400 'Folder doesn't exist'). Accepting both, or neither,
    would read the wrong scope or none."""
    from fwgitops.cli import main
    assert main(["snapshot", "InterfaceRequest", "prod-edge",
                 "--device", "007955000894453", "--out", "/tmp/x.json"]) == 1
    assert "exactly one" in capsys.readouterr().err
    assert main(["snapshot", "InterfaceRequest", "--out", "/tmp/x.json"]) == 1
    assert "exactly one" in capsys.readouterr().err


# ── apply-order: roots sequenced by the registry, not by glob ─────────────
def _tfvars(root, name, files):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("{}")
    return d


def test_apply_order_sequences_roots_by_the_kinds_they_hold(tmp_path, capsys):
    """A root holding interfaces must precede one holding zones/routes/rules —
    even when the alphabet says otherwise, which is what the old
    `for dir in terraform/*/` relied on."""
    from fwgitops.cli import run_apply_order
    _tfvars(tmp_path, "aaa-folder", ["zones.auto.tfvars.json", "rules.auto.tfvars.json"])
    _tfvars(tmp_path, "zzz-device", ["interfaces.auto.tfvars.json"])
    assert run_apply_order(tmp_path) == 0
    assert capsys.readouterr().out.split() == ["zzz-device", "aaa-folder"]


def test_apply_order_skips_roots_with_nothing_emitted(tmp_path, capsys):
    from fwgitops.cli import run_apply_order
    _tfvars(tmp_path, "empty", [])
    _tfvars(tmp_path, "real", ["interfaces.auto.tfvars.json"])
    assert run_apply_order(tmp_path) == 0
    assert capsys.readouterr().out.split() == ["real"]


def test_apply_order_skips_bootstrap_and_module_dirs(tmp_path, capsys):
    """Those carry local state or no state at all; applying them in the chain
    would be wrong regardless of ordering."""
    from fwgitops.cli import run_apply_order
    _tfvars(tmp_path, "modules", ["zones.auto.tfvars.json"])
    _tfvars(tmp_path, "bootstrap-backend", ["zones.auto.tfvars.json"])
    _tfvars(tmp_path, "prod", ["zones.auto.tfvars.json"])
    assert run_apply_order(tmp_path) == 0
    assert capsys.readouterr().out.split() == ["prod"]


def test_apply_order_fails_closed_when_kinds_are_interleaved(tmp_path, capsys):
    """If root A holds a kind depending on one in root B, and B holds a kind
    depending on one in A, NO whole-root order works. Emitting an arbitrary
    sequence would look like success and apply things in the wrong order."""
    from fwgitops.cli import run_apply_order
    _tfvars(tmp_path, "one", ["interfaces.auto.tfvars.json", "rules.auto.tfvars.json"])
    _tfvars(tmp_path, "two", ["zones.auto.tfvars.json"])
    assert run_apply_order(tmp_path) == 2
    err = capsys.readouterr().err
    assert "no whole-root apply order" in err
    assert "per-kind applies" in err


# ── deleting the last intent of a kind must delete its tfvars file ────────
def _zone_intent(zone="dmz"):
    return (
        "apiVersion: fw-intent/v1\n"
        "kind: ZoneRequest\n"
        "metadata: {id: ZONE-9, requester: m@corp, ticket: J-9, justification: x,"
        " requested: 2026-08-05}\n"
        f"spec: {{environment: prod, zone: {zone}, type: layer3, interfaces: []}}\n"
    )


def test_removing_the_last_intent_of_a_kind_removes_its_tfvars_file(tmp_path):
    """Writing the files a compile produces is not enough. The PREVIOUS file
    stays on disk, Terraform auto-loads it, and the deleted object is silently
    re-asserted — a real deletion reads as `No changes`.

    CI never saw this (the files are gitignored, so a clean checkout has none),
    which is what makes it worth a test: it only bites the person verifying a
    deletion by hand, and it gives them a confident wrong answer.
    """
    intents = tmp_path / "intent" / "prod"
    intents.mkdir(parents=True)
    (intents / "ZONE.yaml").write_text(_zone_intent())
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"

    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0
    zones = out / "prod-edge" / "zones.auto.tfvars.json"
    assert zones.exists()

    (intents / "ZONE.yaml").unlink()
    (intents / "RULE.yaml").write_text(VALID_INTENT)          # keep the folder alive
    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0
    assert not zones.exists(), "stale zones tfvars survived — the zone would be re-asserted"


def test_a_scope_that_loses_every_intent_is_swept_too(tmp_path):
    """The sharpest case, and the one a `written`-scoped sweep would miss: when
    a folder loses its LAST intent of ANY kind, the compile writes nothing there,
    so that directory is never visited unless the sweep looks wider."""
    intents = tmp_path / "intent" / "prod"
    intents.mkdir(parents=True)
    (intents / "ZONE.yaml").write_text(_zone_intent())
    (tmp_path / "other").mkdir()
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"

    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0
    zones = out / "prod-edge" / "zones.auto.tfvars.json"
    assert zones.exists()

    # every intent gone for prod-edge, but ANOTHER folder still compiles, so the
    # run does not take the "no intent files" early exit
    (intents / "ZONE.yaml").unlink()
    other = tmp_path / "intent" / "staging"
    other.mkdir()
    (other / "ZONE.yaml").write_text(_zone_intent(zone="dmz2").replace(
        "environment: prod", "environment: staging"))
    env_map.write_text(ENV_MAP + "staging:\n  folder: staging-edge\n"
                       "  from_zone: local\n  to_zone: internet\n")
    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0
    assert not zones.exists(), "a scope that lost every intent kept its stale tfvars"
    assert (out / "staging-edge" / "zones.auto.tfvars.json").exists()


def test_the_sweep_never_deletes_a_file_the_compiler_does_not_own(tmp_path):
    """Only a registered kind's exact tfvars filename is removable. Someone may
    hand-maintain another `*.auto.tfvars.json` in the same root, and deleting a
    file we did not write is not cleanup, it is data loss."""
    intents = tmp_path / "intent" / "prod"
    intents.mkdir(parents=True)
    (intents / "RULE.yaml").write_text(VALID_INTENT)
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP)
    out = tmp_path / "terraform"
    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0

    foreign = out / "prod-edge" / "operator-overrides.auto.tfvars.json"
    foreign.write_text('{"hand": "maintained"}\n')
    assert run_compile(tmp_path / "intent", env_map, out, require_terraform_root=False) == 0
    assert foreign.exists(), "the sweep deleted a file the compiler never wrote"


# ── objects compiled into a folder no firewall inherits ───────────────────
_HIER_ONE_EMPTY = """
folders:
  ngfw-shared:
    children: [prod-edge, GitOps]
    targetable: false
  prod-edge:
    children: []
    targetable: true
    devices:
      "007955000894453": {display_name: fw-a, model: PA-VM, targetable: true}
  GitOps:
    children: []
    targetable: true
"""


def _compile_into(tmp_path, folder, capsys, monkeypatch):
    cat = tmp_path / "catalog"
    cat.mkdir()
    (cat / "folders.yaml").write_text(_HIER_ONE_EMPTY)
    monkeypatch.chdir(tmp_path)
    intents = tmp_path / "intent" / "prod"
    intents.mkdir(parents=True)
    (intents / "R.yaml").write_text(VALID_INTENT)
    env_map = tmp_path / "env.yaml"
    env_map.write_text(f"prod:\n  folder: {folder}\n  from_zone: trust\n  to_zone: app\n")
    rc = run_compile(tmp_path / "intent", env_map, tmp_path / "terraform",
                     require_terraform_root=False)
    return rc, capsys.readouterr()


def test_compiling_into_a_folder_with_no_firewall_WARNS(tmp_path, capsys, monkeypatch):
    """The quietest failure this pipeline can produce: compile succeeds, apply
    succeeds, the push succeeds trivially because there is nothing to push to,
    and not one packet is filtered. Every signal green, rule enforced nowhere."""
    rc, cap = _compile_into(tmp_path, "GitOps", capsys, monkeypatch)
    assert rc == 0
    assert "has NO FIREWALL beneath it" in cap.err
    assert "enforce nothing" in cap.err


def test_it_is_a_warning_not_a_rejection(tmp_path, capsys, monkeypatch):
    """ADR-0002 creates the folder BEFORE the firewall registers to it (the
    firewall names it as `dgname`), so an empty folder is the normal state during
    bring-up. Failing here would break the documented Day-1 order."""
    rc, _ = _compile_into(tmp_path, "GitOps", capsys, monkeypatch)
    assert rc == 0
    assert (tmp_path / "terraform" / "GitOps" / "rules.auto.tfvars.json").exists()


def test_no_warning_for_a_folder_that_has_a_firewall(tmp_path, capsys, monkeypatch):
    """A warning that fires on the normal case is one people stop reading."""
    rc, cap = _compile_into(tmp_path, "prod-edge", capsys, monkeypatch)
    assert rc == 0
    assert "NO FIREWALL" not in cap.err
