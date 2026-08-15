"""RouteRequest — intent kind #4 (ADR-0001).

The first kind where one intent does NOT map to one object. A static route lives
at `vrf[].routing_table.ip.static_route[]` inside `scm_logical_router`, and that
same object carries the VRF's interface membership. Terraform manages whole
objects, so the compiler AGGREGATES.

It is also the first kind whose failure mode is an outage rather than a no-op:
an empty zone drops nothing, but a router written without its interface list
breaks routing for everything behind the firewall. Most of what is tested here
is that guarantee.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.catalog import RouterCatalog  # noqa: E402
from fwgitops.classify import classify_route  # noqa: E402
from fwgitops.compiler import CompiledRoute, CompileError, route_tfvars  # noqa: E402
from fwgitops.intent import IntentError, load_intent  # noqa: E402
from fwgitops.kinds import REGISTRY, compile_any  # noqa: E402
from fwgitops.resolve import EnvMap  # noqa: E402
from fwgitops.tfcontract import (  # noqa: E402
    _emitted_paths, check_contract, check_object_attributes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _doc(_id="RT-1", **spec):
    base = {"environment": "prod", "destination": "0.0.0.0/0", "nexthop": "10.20.0.254"}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "RouteRequest",
        "metadata": {"id": _id, "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def _env():
    return EnvMap.from_dict(
        {"prod": {"folder": "prod-edge", "from_zone": "local", "to_zone": "internet"}})


def _cat(interfaces=("$eth-local", "$eth-internet")):
    return RouterCatalog.from_dict({"routers": {"prod-edge": {"default": {
        "vrfs": {"default": {"interfaces": list(interfaces)}}}}}})


def _load(**kw):
    doc = kw.pop("doc", None) or _doc(**kw)
    return load_intent(doc, router_catalog=_cat(), env_map=_env())


# ── schema ────────────────────────────────────────────────────────────────
def test_a_default_route_loads_with_membership_resolved():
    sp = _load().spec
    assert sp.destination == "0.0.0.0/0" and sp.nexthop == "10.20.0.254"
    assert sp.router == "default" and sp.vrf == "default"
    # Resolved from the catalog at load time, NOT requester input.
    assert sp.vrf_interfaces == ("$eth-local", "$eth-internet")


def test_a_next_hop_interface_route_loads():
    sp = _load(nexthop=None, nexthop_interface="$eth-internet").spec
    assert sp.nexthop is None and sp.nexthop_interface == "$eth-internet"


def test_exactly_one_next_hop_is_required():
    """Both is ambiguous; neither is a discard route the requester did not ask
    for. Either way the device does something other than what was written."""
    with pytest.raises(IntentError, match="exactly one"):
        _load(nexthop_interface="$eth-internet")          # both
    # "Neither" gets a constructive message rather than a restated rule — the
    # requester's next action is to add a field, so the error names the fields.
    with pytest.raises(IntentError, match="set a next hop"):
        _load(nexthop=None)                                # neither


@pytest.mark.parametrize("bad,frag", [
    ({"destination": "10.0.0.1"}, "CIDR"),
    ({"destination": "not-a-network/24"}, "CIDR"),
    ({"destination": ""}, "destination"),
    ({"metric": 0}, "positive integer"),
    ({"metric": "low"}, "positive integer"),
    ({"admin_dist": -1}, "positive integer"),
])
def test_bad_shapes_are_rejected(bad, frag):
    with pytest.raises(IntentError) as e:
        _load(**bad)
    assert any(frag in str(p) for p in e.value.problems)


def test_an_undeclared_router_is_rejected():
    """Fail closed: without a catalog entry the compiler has no membership to
    carry, and would emit a router that strips its own interface list."""
    with pytest.raises(IntentError) as e:
        _load(router="wan-edge")
    assert any("wan-edge" in str(p) for p in e.value.problems)


def test_an_undeclared_vrf_is_rejected():
    with pytest.raises(IntentError) as e:
        _load(vrf="dmz")
    assert any("dmz" in str(p) for p in e.value.problems)


# ── aggregation ───────────────────────────────────────────────────────────
def _reasons(verdict):
    return [f["reason"] for f in verdict.checks_fired]


def _compiled(name, dest, **kw):
    base = dict(folder="prod-edge", router="default", vrf="default",
                vrf_interfaces=("$eth-local", "$eth-internet"))
    base.update(kw)
    return CompiledRoute(name=name, destination=dest, **base)


def test_many_routes_become_one_router():
    out = route_tfvars([
        _compiled("RT-1", "0.0.0.0/0", nexthop="10.20.0.254"),
        _compiled("RT-2", "192.168.5.0/24", nexthop="10.20.0.9", metric=20),
    ])["routers"]
    assert list(out) == ["default"]
    vrfs = out["default"]["vrf"]
    assert len(vrfs) == 1
    routes = vrfs[0]["routing_table"]["ip"]["static_route"]
    assert [r["name"] for r in routes] == ["RT-1", "RT-2"]


def test_membership_survives_aggregation():
    """The whole reason catalog/routers.yaml exists. If this list is ever empty
    on a real apply, every interface leaves the VRF and traffic stops."""
    out = route_tfvars([_compiled("RT-1", "0.0.0.0/0", nexthop="10.20.0.254")])
    vrf = out["routers"]["default"]["vrf"][0]
    assert vrf["interface"] == ["$eth-local", "$eth-internet"]


def test_no_routes_emits_no_router():
    """An empty request set must not write an empty router over a live one."""
    assert route_tfvars([]) == {"routers": {}}


def test_a_router_spanning_folders_is_rejected():
    with pytest.raises(CompileError, match="folder"):
        route_tfvars([
            _compiled("RT-1", "0.0.0.0/0", nexthop="10.1.0.1"),
            _compiled("RT-2", "10.9.0.0/24", nexthop="10.1.0.1", folder="lab"),
        ])


def test_disagreeing_membership_is_rejected():
    """Two routes on the same VRF that disagree about who belongs to it means
    one of them would silently win and evict the other's interfaces."""
    with pytest.raises(CompileError, match="interface"):
        route_tfvars([
            _compiled("RT-1", "0.0.0.0/0", nexthop="10.1.0.1"),
            _compiled("RT-2", "10.9.0.0/24", nexthop="10.1.0.1",
                      vrf_interfaces=("$eth-local",)),
        ])


def test_duplicate_route_ids_are_rejected():
    with pytest.raises(CompileError, match="duplicate"):
        route_tfvars([
            _compiled("RT-1", "0.0.0.0/0", nexthop="10.1.0.1"),
            _compiled("RT-1", "10.9.0.0/24", nexthop="10.1.0.1"),
        ])


def test_optional_fields_are_omitted_not_nulled():
    r = route_tfvars([_compiled("RT-1", "10.9.0.0/24", nexthop="10.1.0.1")])
    sr = r["routers"]["default"]["vrf"][0]["routing_table"]["ip"]["static_route"][0]
    assert sr["metric"] is None and sr["admin_dist"] is None


# ── registry ──────────────────────────────────────────────────────────────
def test_the_kind_is_registered_and_compiles_through_the_registry():
    h = REGISTRY["RouteRequest"]
    assert h.drift_engine == "state"          # scm_logical_router has no `tag`
    assert h.state_api_path == "/config/network/v1/logical-routers"
    assert h.tfvars_filename == "routers.auto.tfvars.json"
    c = compile_any(_load(), env_map=_env())
    assert isinstance(c, CompiledRoute) and c.folder == "prod-edge"


# ── risk ──────────────────────────────────────────────────────────────────
def test_a_default_route_is_high_risk():
    v = classify_route(_compiled("RT-1", "0.0.0.0/0", nexthop="10.20.0.254"))
    assert v.tier == "HIGH"
    assert any("default route" in r.lower() for r in _reasons(v))


def test_a_v6_default_route_is_high_risk():
    v = classify_route(_compiled("RT-1", "::/0", nexthop="2001:db8::1"))
    assert v.tier == "HIGH"


def test_a_specific_route_is_not_flagged_as_a_default_route():
    v = classify_route(_compiled("RT-1", "192.168.5.0/24", nexthop="10.20.0.9"))
    assert not any("default route" in r.lower() for r in _reasons(v))


def test_taking_over_an_inherited_router_is_high_risk():
    """Writing a router that is currently inherited from a parent folder makes
    the child folder the owner — a scope change, not just a new route."""
    v = classify_route(
        _compiled("RT-1", "192.168.5.0/24", nexthop="10.20.0.9"),
        current={("prod-edge", "default"): {"folder": "ngfw-shared"}},
    )
    assert v.tier == "HIGH"
    assert any("inherit" in r.lower() or "owner" in r.lower() for r in _reasons(v))


# ── the compiler→Terraform contract (ADR-0004) ────────────────────────────
def test_emitted_paths_recurse_into_lists_of_objects():
    """Regression. `_emitted_paths` used to recurse into dicts only, so a
    list-of-object attribute contributed just its own path — and HOLE 3 passed
    VACUOUSLY for everything below it. `routers` compared only {name, folder,
    vrf} and never once looked at a static-route field. `declared_object_
    attributes` collapses the list level, so the emitted side must too.
    """
    paths = _emitted_paths({"vrf": [{"name": "default",
                                     "routing_table": {"ip": {"static_route": [
                                         {"name": "RT-1", "metric": 10}]}}}]})
    assert "vrf.name" in paths
    assert "vrf.routing_table.ip.static_route.name" in paths
    assert "vrf.routing_table.ip.static_route.metric" in paths


def test_a_list_of_scalars_contributes_no_attribute_names():
    """A string asserts no attribute names, so a list of them adds only its own
    path. (Not a shape the compiler currently emits — `interfaces` wraps its
    addresses as `ip: [{name: ...}]` — but the recursion must not invent paths
    for scalars if a future kind does emit one.)"""
    assert _emitted_paths({"layer3": {"ip": ["10.0.0.1/24"]}}) == {"layer3", "layer3.ip"}


def test_the_root_declares_every_key_and_attribute_the_compiler_emits():
    """HOLE 1 + HOLE 3 against the REAL root, four levels deep. Terraform
    discards undeclared object attributes silently, at any depth."""
    payload = route_tfvars([
        _compiled("RT-1", "0.0.0.0/0", nexthop="10.20.0.254", metric=10, admin_dist=10),
        _compiled("RT-2", "192.168.5.0/24", nexthop_interface="$eth-internet"),
    ])
    root = REPO_ROOT / "terraform" / "prod-edge"
    assert check_contract(root, sorted(payload)) == []
    assert check_object_attributes(root, "routers", payload["routers"]) == []


def test_the_module_and_root_declare_routers_identically():
    """Drift between the two declarations reintroduces HOLE 3 at the module
    boundary: the root accepts an attribute the module then discards."""
    def _block(p):
        s = (REPO_ROOT / p).read_text()
        i = s.index('variable "routers"')
        return s[i:s.index("\n}\n", i)]
    assert _block("terraform/prod-edge/variables.tf") == \
           _block("terraform/modules/security_folder/variables.tf")


def test_the_ROUTER_message_still_wins_over_the_generic_one():
    """`router 'X' spans folders 'A' and 'B'` names the router and both folders.
    A generic scope guard running first would mask it, which is why routes check
    last."""
    with pytest.raises(CompileError, match="spans folders"):
        route_tfvars([
            _compiled("RT-1", "0.0.0.0/0", nexthop="10.1.0.1"),
            _compiled("RT-2", "10.9.0.0/24", nexthop="10.1.0.1", folder="lab"),
        ])
