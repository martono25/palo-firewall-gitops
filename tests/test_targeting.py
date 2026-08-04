"""How a Day-1 intent says WHERE it lands.

`AccessRequest` is authored by app teams and targets an `environment:`, which
the platform maps to a folder — they should never need to know SCM topology.
The Day-1 kinds are authored by network engineers, for whom the folder IS the
intent, and `environment` resolves 1:1 so it cannot address a second folder
without a catalog edit. So those kinds take `folder:` directly.

NOTE: a device is NOT a folder. Targeting one firewall needs a `device:` scope,
which this platform does not implement — see ADR-0006's correction note.

That field is only safe because of the catalog check: unknown or non-targetable
is REJECTED at compile time, not tiered up. HIGH is approvable, and a write to a
shared parent should not be one rubber-stamp away from every device at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.catalog import FolderHierarchy, InterfaceCatalog, RouterCatalog  # noqa: E402
from fwgitops.compiler import compile_request  # noqa: E402
from fwgitops.intent import IntentError, load_intent  # noqa: E402
from fwgitops.kinds import compile_any  # noqa: E402
from fwgitops.resolve import EnvMap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "GitOps"          # a real container folder, targetable
DEVICE = "007955000894453"  # an `on-prem` DEVICE entry — NOT a folder


def _hierarchy():
    return FolderHierarchy.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))


def _env():
    return EnvMap.from_dict(
        {"prod": {"folder": "prod-edge", "from_zone": "local", "to_zone": "internet"}})


def _routers():
    return RouterCatalog.from_dict({"routers": {
        "prod-edge": {"default": {"vrfs": {"default": {"interfaces": ["$eth-local"]}}}},
        SANDBOX: {"default": {"vrfs": {"default": {"interfaces": ["$eth-local"]}}}},
    }})


def _ifcat():
    """`interface:` is a ROLE. `local` resolves to `$eth-local` at folder scope
    and to the physical port at device scope — one object, two names."""
    return InterfaceCatalog.from_dict({"interfaces": {
        "local": {"folder": "$eth-local", "devices": {DEVICE: "ethernet1/4"}},
    }})


def _iface(**spec):
    base = {"interface": "local", "ip": ["10.20.0.1/24"]}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "InterfaceRequest",
        "metadata": {"id": "IF-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def _load(doc, hierarchy=True, **kw):
    return load_intent(
        doc,
        env_map=_env(),
        router_catalog=_routers(),
        interface_catalog=_ifcat(),
        folder_hierarchy=_hierarchy() if hierarchy else None,
        **kw,
    )


# ── the folder: form ──────────────────────────────────────────────────────
def test_a_folder_can_be_targeted_directly():
    """The case `environment:` cannot express without a platform-config edit:
    a second folder, named by the engineer authoring the change."""
    sp = _load(_iface(folder=SANDBOX)).spec
    assert sp.folder == SANDBOX and sp.environment is None
    assert compile_any(_load(_iface(folder=SANDBOX)), env_map=_env()).folder == SANDBOX


def test_a_serial_in_the_folder_field_is_redirected_to_device():
    """v1.11.0 listed the two firewalls as targetable child FOLDERS.
    `folder=<serial>` returns 400 "Folder doesn't exist" — a firewall is the last
    level of the hierarchy but is addressed `device=`. Such an intent compiled
    clean and would have failed only at apply. Now it is caught at the
    requester's door, and the message names the fix."""
    with pytest.raises(IntentError) as e:
        _load(_iface(folder=DEVICE))
    msg = " ".join(str(p) for p in e.value.problems)
    assert "is a FIREWALL, not a folder" in msg
    assert f"device: {DEVICE}" in msg


def test_the_environment_form_still_works():
    sp = _load(_iface(environment="prod")).spec
    assert sp.environment == "prod" and sp.folder is None
    assert compile_any(_load(_iface(environment="prod")), env_map=_env()).folder == "prod-edge"


def test_exactly_one_target_is_required():
    with pytest.raises(IntentError, match="exactly one target"):
        _load(_iface(folder=SANDBOX, environment="prod"))
    with pytest.raises(IntentError, match="set a target"):
        _load(_iface())


# ── the guardrail ─────────────────────────────────────────────────────────
def test_a_shared_parent_is_refused_at_compile_time():
    """`ngfw-shared` parents production AND the sandbox. Refused outright rather
    than tiered up — HIGH is approvable, and this should not be."""
    with pytest.raises(IntentError) as e:
        _load(_iface(folder="ngfw-shared"))
    msg = " ".join(str(p) for p in e.value.problems)
    assert "not targetable" in msg
    # The message must say WHY and what is allowed instead.
    assert "prod-edge" in msg and "GitOps" in msg


def test_an_undeclared_folder_is_refused():
    """Fail closed: a typo'd or newly created folder must not inherit permission
    by default. Declaring it is the only way in."""
    with pytest.raises(IntentError, match="not declared in catalog/folders.yaml"):
        _load(_iface(folder="prod-edge-2"))


def test_folder_is_unusable_without_the_catalog_rather_than_unchecked():
    """The dangerous failure would be treating a missing catalog as "no check
    needed" and letting any folder through."""
    with pytest.raises(IntentError, match="Refusing to target an unchecked scope"):
        _load(_iface(folder=SANDBOX), hierarchy=False)


def test_the_environment_path_is_unaffected_by_targetability():
    """`catalog/environments.yaml` is reviewed platform config, not requester
    input, so it is not subject to the requester-facing guardrail. The threat
    model is the field a requester writes."""
    env = EnvMap.from_dict(
        {"shared": {"folder": "ngfw-shared", "from_zone": "local", "to_zone": "internet"}})
    req = load_intent(_iface(environment="shared"), env_map=env,
                      folder_hierarchy=_hierarchy(), router_catalog=_routers(),
                      interface_catalog=_ifcat())
    assert compile_any(req, env_map=env).folder == "ngfw-shared"


# ── AccessRequest keeps app-language addressing ───────────────────────────
def _access(**spec):
    base = {"environment": "prod", "action": "allow",
            "source": [{"cidr": "10.20.0.0/24"}],
            "destination": [{"cidr": "10.30.0.0/24"}],
            "service": [{"protocol": "tcp", "port": "443"}]}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "AccessRequest",
        "metadata": {"id": "AR-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def test_an_access_request_naming_a_folder_is_rejected_not_ignored():
    """Regression. `folder:` was silently ignored here, so an AccessRequest that
    copied it from a Day-1 example landed in whatever `environment` resolved to
    while its author believed otherwise — a silently wrong target."""
    with pytest.raises(IntentError) as e:
        _load(_access(folder=SANDBOX))
    assert any("environment" in str(p) for p in e.value.problems)


def test_a_plain_access_request_is_unaffected():
    req = _load(_access())
    assert req.spec.environment == "prod"
    assert compile_request(req, _env()).rule.folder == "prod-edge"


# ── routers.yaml is keyed by the resolved folder ──────────────────────────
def _route(**spec):
    base = {"destination": "0.0.0.0/0", "nexthop": "10.20.0.254"}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "RouteRequest",
        "metadata": {"id": "RT-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def test_router_membership_is_looked_up_under_the_targeted_folder():
    """Both addressing forms must reach the same catalog lookup, or a route
    targeted by folder would aggregate without its VRF interface membership."""
    assert _load(_route(folder=SANDBOX)).spec.vrf_interfaces == ("$eth-local",)
    assert _load(_route(environment="prod")).spec.vrf_interfaces == ("$eth-local",)


def test_a_folder_with_no_declared_router_is_rejected():
    cat = RouterCatalog.from_dict({"routers": {
        "prod-edge": {"default": {"vrfs": {"default": {"interfaces": ["$eth-local"]}}}}}})
    with pytest.raises(IntentError, match="not declared for folder 'GitOps'"):
        load_intent(_route(folder="GitOps"), env_map=_env(),
                    router_catalog=cat, folder_hierarchy=_hierarchy())


# ── folder-scope-only kinds reject `device:` at PR time ───────────────────
def _zone(**spec):
    base = {"zone": "dmz", "type": "layer3", "interfaces": []}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "ZoneRequest",
        "metadata": {"id": "ZN-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-05"},
        "spec": base,
    }


def test_a_zone_cannot_target_a_firewall():
    """Verified live 2026-08-05 (spike/zone-device-scope): SCM refuses a zone at
    device scope with "Device <serial> doesn't exist" — while the SAME
    device-scope write of an ethernet interface on the SAME firewall succeeds.

    Rejected here so the failure is a PR comment naming the real constraint,
    rather than an apply-time error blaming a firewall that is present and
    connected — which sends the reader after entirely the wrong problem.
    """
    with pytest.raises(IntentError, match="a zone cannot target a firewall"):
        _load(_zone(device=DEVICE))


def test_a_zone_can_still_target_a_folder():
    """The guard must reject the SCOPE, not the kind. Without this, disabling
    device targeting could quietly disable zone targeting altogether."""
    assert _load(_zone(folder=SANDBOX)).spec.folder == SANDBOX


def test_the_folder_only_message_names_the_kind_that_was_rejected():
    """Two kinds share this guard now. A shared message that said "route" for a
    zone would be actively misleading — the reader would look for a route."""
    with pytest.raises(IntentError, match="a route cannot target a firewall"):
        _load(_route(device=DEVICE))
    with pytest.raises(IntentError, match="logical routers at FOLDER scope"):
        _load(_route(device=DEVICE))
    with pytest.raises(IntentError, match="zones at FOLDER scope"):
        _load(_zone(device=DEVICE))


# ── the classifier must key on SCOPE, not folder ──────────────────────────
def test_a_device_scoped_change_is_still_risk_checked():
    """Regression, and a bad one.

    `interface_becomes_addressed` (HIGH) looks the interface up in live state to
    tell "puts it on a network" from "edits an existing address". It keyed on
    `.folder`, which is None for a device-scoped object — so the lookup always
    missed, the check never fired, and putting a production firewall's interface
    on a network for the first time reported LOW with no checks. Fail-open in
    the risk direction, introduced by the Scope change.
    """
    from fwgitops.classify import classify_interface
    from fwgitops.compiler import CompiledInterface

    iface = CompiledInterface(device=DEVICE, name="ethernet1/4", ip=["10.20.0.1/24"])
    # Keyed exactly as `snapshot` stamps it and `drift` builds it.
    live = {(f"device:{DEVICE}", "ethernet1/4"): {"name": "ethernet1/4", "layer3": {}}}

    v = classify_interface(iface, hierarchy=_hierarchy(), current=live)
    assert v.tier == "HIGH"
    assert [c["check"] for c in v.checks_fired] == ["interface_becomes_addressed"]


def test_an_already_addressed_device_interface_is_not_the_populating_case():
    """The other half: editing an existing address is a real change but not the
    same act, so it must not borrow the populating check's tier."""
    from fwgitops.classify import classify_interface
    from fwgitops.compiler import CompiledInterface

    iface = CompiledInterface(device=DEVICE, name="ethernet1/4", ip=["10.20.0.2/24"])
    live = {(f"device:{DEVICE}", "ethernet1/4"): {
        "name": "ethernet1/4", "layer3": {"ip": [{"name": "10.20.0.1/24"}]}}}
    v = classify_interface(iface, hierarchy=_hierarchy(), current=live)
    assert "interface_becomes_addressed" not in [c["check"] for c in v.checks_fired]


def test_folder_fan_out_is_deliberately_skipped_for_a_firewall():
    """Targeting one firewall is the NARROWEST act — a device write creates a
    per-device override and reaches nothing else. There is no fan-out to warn
    about, so the check is not applied on purpose rather than by accident."""
    from fwgitops.classify import _blast_radius
    from fwgitops.compiler import CompiledZone

    on_device = CompiledZone(device=DEVICE, name="z", zone_type="layer3", interfaces=[])
    in_shared = CompiledZone(folder="ngfw-shared", name="z", zone_type="layer3",
                             interfaces=[])
    assert _blast_radius(on_device, _hierarchy()) is None
    fired = _blast_radius(in_shared, _hierarchy())
    assert fired is not None and fired["tier"] == "HIGH"


def test_the_scope_key_matches_across_classify_drift_and_snapshot():
    """These three build the same key independently. If they drift apart, a
    lookup silently misses and the check it guards never fires."""
    from fwgitops.classify import _scope_key
    from fwgitops.compiler import CompiledZone, scope_of

    on_device = CompiledZone(device=DEVICE, name="z", zone_type="layer3", interfaces=[])
    in_folder = CompiledZone(folder="prod-edge", name="z", zone_type="layer3", interfaces=[])
    assert _scope_key(on_device) == scope_of(on_device).key == f"device:{DEVICE}"
    assert _scope_key(in_folder) == scope_of(in_folder).key == "prod-edge"
