"""The intent-kind registry (ADR-0001).

These tests exist because the registry's whole purpose is to make a half-wired
kind impossible. `ZoneRequest` shipped into three stages and was silently absent
from four, and Terraform's exit-0 on an undeclared variable meant nothing
noticed for a release (ADR-0004). So the tests assert the REGISTRY is complete
and self-consistent, not just that dispatch works.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.compiler import CompileError, CompiledChange, CompiledZone  # noqa: E402
from fwgitops.kinds import (  # noqa: E402
    REGISTRY,
    compile_any,
    group_by_kind_and_scope,
    handler_for_compiled,
    handler_for_request,
    kinds_with_drift_engine,
    of_kind,
    registered_tfvars_filenames,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── the registry must be internally consistent ────────────────────────────
@pytest.mark.parametrize("kind", sorted(REGISTRY))
def test_every_handler_is_fully_populated(kind):
    """A handler with a missing field is a kind wired into some stages and not
    others — exactly the shape that shipped broken."""
    h = REGISTRY[kind]
    assert h.kind == kind
    for attr in ("request_type", "compiled_type", "compile", "tfvars_filename",
                 "tfvars", "scope_of", "name_of", "classify",
                 "evidence_object", "evidence_id_of"):
        assert getattr(h, attr) is not None, f"{kind}.{attr} is not set"
    assert h.drift_engine in ("tag", "state"), f"{kind}: undeclared drift engine"


def test_kinds_match_the_intent_loaders_exactly():
    """The loader registry and the kind registry must not drift apart — a kind
    that loads but has no handler compiles into nothing."""
    from fwgitops.intent import _KIND_LOADERS
    assert set(REGISTRY) == set(_KIND_LOADERS)


def test_tfvars_filenames_are_unique_per_kind():
    """Two kinds sharing a filename would silently overwrite each other."""
    names = list(registered_tfvars_filenames().values())
    assert len(names) == len(set(names))


def test_every_tfvars_filename_matches_the_gitignore_glob():
    """Compiled output is a build artifact. The glob must cover every kind,
    including ones added later."""
    import subprocess
    for filename in registered_tfvars_filenames().values():
        rel = f"terraform/prod-edge/{filename}"
        rc = subprocess.run(["git", "check-ignore", "-q", rel],
                            cwd=REPO_ROOT).returncode
        assert rc == 0, f"{rel} is NOT gitignored — it would be committed as source"


def test_compiled_types_are_distinct():
    """handler_for_compiled resolves by isinstance, so overlapping types would
    make dispatch order-dependent."""
    types = [h.compiled_type for h in REGISTRY.values()]
    assert len(types) == len(set(types))
    for a in types:
        for b in types:
            if a is not b:
                assert not issubclass(a, b), f"{a.__name__} subclasses {b.__name__}"


# ── dispatch ──────────────────────────────────────────────────────────────
def test_handler_for_request_and_compiled_agree():
    from test_intent import valid_doc
    from fwgitops.intent import load_intent
    from fwgitops.resolve import EnvMap
    em = EnvMap.from_dict({"prod": {"folder": "f", "from_zone": "a", "to_zone": "b"}})
    req = load_intent(valid_doc())
    h1 = handler_for_request(req)
    h2 = handler_for_compiled(compile_any(req, em))
    assert h1.kind == h2.kind == "AccessRequest"


@pytest.mark.parametrize("obj", [object(), "string", 42, None])
def test_dispatch_fails_closed_on_an_unregistered_type(obj):
    with pytest.raises(CompileError, match="no kind registered"):
        handler_for_compiled(obj)
    with pytest.raises(CompileError, match="no kind registered"):
        handler_for_request(obj)


def test_of_kind_filters_without_isinstance_at_the_call_site():
    rules = [CompiledChange(address_objects=[], service_objects=[], rule=None)]
    zones = [CompiledZone(folder="f", name="z", zone_type="layer3", interfaces=[])]
    mixed = rules + zones
    assert of_kind(mixed, "ZoneRequest") == zones
    assert of_kind(mixed, "AccessRequest") == rules


def test_group_by_kind_and_scope_keys_on_both():
    from fwgitops.compiler import Scope
    a = CompiledZone(folder="f1", name="z1", zone_type="layer3", interfaces=[])
    b = CompiledZone(folder="f2", name="z2", zone_type="layer3", interfaces=[])
    c = CompiledZone(folder="f1", name="z3", zone_type="layer3", interfaces=[])
    grouped = group_by_kind_and_scope([a, b, c])
    assert grouped[("ZoneRequest", Scope("folder", "f1"))] == [a, c]
    assert grouped[("ZoneRequest", Scope("folder", "f2"))] == [b]


def test_a_firewall_scope_never_merges_with_its_folders():
    """A device write is a per-device OVERRIDE of a different object, not an
    edit of the folder's — so they must not share a group or a Terraform
    state."""
    from fwgitops.compiler import Scope
    in_folder = CompiledZone(folder="prod-edge", name="z", zone_type="layer3", interfaces=[])
    on_device = CompiledZone(device="007955000902404", name="z", zone_type="layer3",
                             interfaces=[])
    grouped = group_by_kind_and_scope([in_folder, on_device])
    assert grouped[("ZoneRequest", Scope("folder", "prod-edge"))] == [in_folder]
    assert grouped[("ZoneRequest", Scope("device", "007955000902404"))] == [on_device]
    # And they land in different Terraform roots.
    assert Scope("device", "007955000902404").dirname == "device-007955000902404"
    assert Scope("folder", "prod-edge").dirname == "prod-edge"


# ── capability is DECLARED, not faked ─────────────────────────────────────
def test_drift_engines_are_declared_per_kind_not_assumed_uniform():
    """Rules carry gitops: tags so drift knows WHO created something. scm_zone
    has no tag attribute, so zones use state-based drift. Same word, genuinely
    different mechanism — the registry records which, rather than pretending
    one signature fits both."""
    tag = {h.kind for h in kinds_with_drift_engine("tag")}
    state = {h.kind for h in kinds_with_drift_engine("state")}
    # Rules are the ONLY taggable kind — scm_zone and scm_ethernet_interface
    # both lack a `tag` attribute, and only 14 of the provider's resources have
    # one. Tag-based drift is the exception, not the default.
    assert tag == {"AccessRequest"}
    assert {"ZoneRequest", "InterfaceRequest"} <= state
    assert tag | state == set(REGISTRY), "every kind must declare a drift engine"


def test_every_kind_can_produce_an_evidence_object():
    """`has_evidence` used to be False for three of four kinds, and the honesty of
    declaring that hid what it cost: ten intents produced five bundles, so a route
    or interface change left no audit record while the command exited 0.

    A kind is not shippable without an audit record, so this is a hard assertion
    on ALL of them rather than a flag anyone can set False."""
    from fwgitops.compiler import CompiledInterface, CompiledRoute

    samples = {
        "ZoneRequest": CompiledZone(folder="f", name="dmz", zone_type="layer3",
                                    interfaces=["$eth-local"]),
        "InterfaceRequest": CompiledInterface(folder="f", name="$eth-local",
                                              ip=["10.0.0.1/24"]),
        "RouteRequest": CompiledRoute(folder="f", router="r", vrf="v",
                                      name="REQ-1", destination="0.0.0.0/0"),
    }
    for kind, obj in samples.items():
        payload = REGISTRY[kind].evidence_object(obj)
        assert payload, f"{kind} produced an empty evidence object"
        # Scope is recorded ONCE, as compiled.scope. Repeating it inside the
        # object would let a bundle disagree with itself about which firewall a
        # change landed on.
        assert "folder" not in payload and "device" not in payload


def test_the_default_evidence_object_cannot_go_stale():
    """The v1 bundle listed rule fields BY HAND and the list fell behind twice.
    Serialising the dataclass whole means a field added to a compiled type
    reaches the audit record without a second edit — so this asserts coverage is
    derived, not remembered."""
    import dataclasses

    from fwgitops.compiler import CompiledRoute
    r = CompiledRoute(folder="f", router="r", vrf="v", name="REQ-1",
                      destination="0.0.0.0/0", nexthop="10.0.0.254", metric=10)
    payload = REGISTRY["RouteRequest"].evidence_object(r)
    expected = {f.name for f in dataclasses.fields(r)} - {"folder", "device"}
    assert set(payload) == expected


def test_a_handler_callable_is_not_silently_bound_as_a_method():
    """A bare function assigned as a dataclass DEFAULT becomes a class attribute,
    and Python binds it — `handler.evidence_id_of(obj)` would pass the handler as
    `obj` and compare a KindHandler to a request id. `default_factory` avoids it;
    this proves the defaults actually behave like plain functions."""
    z = CompiledZone(folder="f", name="dmz", zone_type="layer3", interfaces=[])
    assert REGISTRY["ZoneRequest"].evidence_id_of(z) is None
    assert "name" in REGISTRY["ZoneRequest"].evidence_object(z)


def test_a_new_kind_needs_exactly_one_registry_entry():
    """The point of the whole refactor: adding a kind is one registration, and
    everything the CLI does is driven off it."""
    h = REGISTRY["ZoneRequest"]
    z = CompiledZone(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=[])
    assert h.scope_of(z).key == "prod-edge"
    assert h.name_of(z) == "dmz"
    assert "zones" in h.tfvars([z])
    assert h.tfvars_filename.endswith(".auto.tfvars.json")


# ── cross-kind ordering (ADR-0002's last unbuilt piece) ───────────────────
def test_the_declared_order_is_adr_0002s_chain():
    from fwgitops.kinds import kind_apply_order
    assert kind_apply_order() == [
        "InterfaceRequest", "ZoneRequest", "RouteRequest", "AccessRequest"]


def test_the_order_is_deterministic():
    """Two runs must agree, or a Day-1 build is not reproducible and the
    ordering is decoration. Ties break alphabetically rather than on dict order."""
    from fwgitops.kinds import kind_apply_order
    assert kind_apply_order() == kind_apply_order()


def test_a_cycle_fails_closed():
    """An unorderable registry must raise, not return an arbitrary sequence —
    the whole point is that the order is guaranteed."""
    from fwgitops.kinds import KindOrderError, REGISTRY, kind_apply_order
    import dataclasses
    a = dataclasses.replace(REGISTRY["ZoneRequest"], depends_on_kinds=("RouteRequest",))
    b = dataclasses.replace(REGISTRY["RouteRequest"], depends_on_kinds=("ZoneRequest",))
    with pytest.raises(KindOrderError, match="cycle"):
        kind_apply_order({"ZoneRequest": a, "RouteRequest": b})


def test_a_dependency_on_an_unregistered_kind_fails_closed():
    """A dropped edge would order things wrongly while looking like it worked —
    exactly the failure this mechanism exists to prevent."""
    from fwgitops.kinds import KindOrderError, REGISTRY, kind_apply_order
    import dataclasses
    h = dataclasses.replace(REGISTRY["ZoneRequest"], depends_on_kinds=("NatRequest",))
    with pytest.raises(KindOrderError, match="unregistered kind"):
        kind_apply_order({"ZoneRequest": h})


def test_every_dependency_names_a_registered_kind():
    """Guards the SHIPPED registry, so a typo in a new kind's dependency is a
    test failure rather than a silently skipped step."""
    from fwgitops.kinds import REGISTRY
    for kind, h in REGISTRY.items():
        for dep in h.depends_on_kinds:
            assert dep in REGISTRY, f"{kind} depends on unregistered {dep!r}"


def test_dependencies_are_consistent_with_the_order():
    """Each kind must appear after everything it declares a dependency on."""
    from fwgitops.kinds import REGISTRY, kind_apply_order
    order = kind_apply_order()
    for kind, h in REGISTRY.items():
        for dep in h.depends_on_kinds:
            assert order.index(dep) < order.index(kind), f"{dep} must precede {kind}"
