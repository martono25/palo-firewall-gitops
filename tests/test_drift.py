"""Tests for tag-based drift detection."""

from __future__ import annotations

from fwgitops.drift import ActualRule, detect_drift
from fwgitops.tags import MANAGED_TAG, Section, managed_tags

from test_classify import _change


def mtags(req_id):
    return tuple(managed_tags(req_id=req_id, section=Section.SPECIFIC_ALLOW, ticket="T-1"))


def desired(*names, folder="prod-edge"):
    return [
        _change(srcs=[("s", "ip-netmask", "10.0.0.0/24")], dsts=[("d", "ip-netmask", "10.0.1.0/24")],
                services=[("svc", "tcp", "443")], name=n, folder=folder)
        for n in names
    ]


def test_clean_when_actual_matches_declared():
    d = desired("R1", "R2")
    actual = [ActualRule("prod-edge", "R1", mtags("R1")), ActualRule("prod-edge", "R2", mtags("R2"))]
    r = detect_drift(d, actual)
    assert r.is_clean and r.count == 0


def test_unmanaged_rule_detected():
    d = desired("R1")
    actual = [ActualRule("prod-edge", "R1", mtags("R1")),
              ActualRule("prod-edge", "MANUAL", ("some:other-tag",))]
    r = detect_drift(d, actual)
    assert [x.name for x in r.unmanaged] == ["MANUAL"]
    assert not r.is_clean


def test_orphaned_managed_rule_detected():
    d = desired("R1")  # R-OLD is managed but no longer declared
    actual = [ActualRule("prod-edge", "R1", mtags("R1")),
              ActualRule("prod-edge", "R-OLD", mtags("R-OLD"))]
    assert [x.name for x in detect_drift(d, actual).orphaned] == ["R-OLD"]


def test_malformed_managed_rule_detected():
    actual = [ActualRule("prod-edge", "BROKEN", (MANAGED_TAG,))]  # marker, no req tag
    assert [x.name for x in detect_drift(desired("R1"), actual).malformed] == ["BROKEN"]


def test_scoped_by_folder():
    # a managed rule with a declared name but in a DIFFERENT folder is orphaned there
    actual = [ActualRule("other", "R1", mtags("R1"))]
    assert [x.name for x in detect_drift(desired("R1", folder="prod-edge"), actual).orphaned] == ["R1"]


def test_summary_lists_drift():
    s = detect_drift(desired("R1"), [ActualRule("prod-edge", "MANUAL", ("x:y",))]).summary()
    assert "DRIFT" in s and "unmanaged" in s and "MANUAL" in s


def test_summary_clean():
    assert "no drift" in detect_drift(desired("R1"),
                                      [ActualRule("prod-edge", "R1", mtags("R1"))]).summary()


# ── State-based drift for untaggable objects (zones, interfaces) ───────────
from fwgitops.compiler import CompiledZone  # noqa: E402
from fwgitops.drift import (  # noqa: E402
    ActualObject,
    declared_state,
    detect_object_drift,
)
from fwgitops.kinds import REGISTRY  # noqa: E402


def _zone(**kw):
    base = dict(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=[])
    base.update(kw)
    return CompiledZone(**base)


def _actual(name="dmz", folder="prod-edge", scope=None, **fields):
    base = {"name": name, "folder": folder,
            "network": {"layer3": [], "zone_protection_profile": None, "log_setting": None},
            "enable_user_identification": None}
    base.update(fields)
    return ActualObject(kind="zone", folder=folder, name=name, fields=base, scope=scope)


def test_a_declared_zone_matching_scm_is_clean():
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone(protection_profile="best-practice")])
    actual = [_actual(network={"layer3": [], "zone_protection_profile": "best-practice",
                               "log_setting": None})]
    assert detect_object_drift(declared, actual).is_clean


def test_a_field_changed_out_of_band_is_modified():
    """Someone turning User-ID off on a managed zone. terraform plan would also
    catch this, but only with intact state — this is independent of that."""
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone(user_id=True)])
    r = detect_object_drift(declared, [_actual(enable_user_identification=False)])
    assert len(r.modified) == 1
    d = r.modified[0]
    assert d.field_name == "enable_user_identification"
    assert d.declared is True and d.actual is False


def test_a_nested_field_is_compared_by_flattened_path():
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone(protection_profile="best-practice")])
    r = detect_object_drift(declared, [_actual(
        network={"layer3": [], "zone_protection_profile": "something-else", "log_setting": None})])
    assert [d.field_name for d in r.modified] == ["network.zone_protection_profile"]


def test_fields_the_declaration_does_not_set_are_not_drift():
    """A None in the declaration means 'we did not ask'. Comparing it would flag
    every provider default as a difference."""
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone()])            # nothing asserted
    r = detect_object_drift(declared, [_actual(
        enable_user_identification=True,                  # SCM's own value
        network={"layer3": [], "zone_protection_profile": "x", "log_setting": "y"})])
    assert r.is_clean


def test_a_declared_zone_absent_from_scm_is_missing():
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone()])
    r = detect_object_drift(declared, [])
    assert r.missing == (("object", "prod-edge", "dmz"),)


def test_a_local_zone_nobody_declared_is_unexpected():
    r = detect_object_drift({}, [_actual(name="rogue")])
    assert [o.name for o in r.unexpected] == ["rogue"]


def test_a_baseline_zone_is_not_unexpected():
    """baseline_zones names objects that legitimately pre-date GitOps — that
    allowlist is what makes `unexpected` meaningful rather than noise."""
    r = detect_object_drift({}, [_actual(name="internet")],
                            baseline={"prod-edge": {"internet", "local"}})
    assert r.is_clean


# ── inheritance: the false-positive class found against the live tenant ────
def test_an_inherited_object_is_not_this_folders_drift():
    """REGRESSION. SCM returns the folder an object is DEFINED in, not the one
    queried. Every zone on the live tenant is defined in the shared parent, so
    keying on the returned folder reported all 7 as unexpected — platform config
    this folder inherits and does not own."""
    r = detect_object_drift({}, [_actual(name="internet", folder="ngfw-shared", scope="prod-edge")])
    assert r.is_clean
    assert len(r.inherited) == 1
    assert "inherited" in r.summary() and "ngfw-shared" in r.summary()


def test_an_inherited_declared_object_is_not_reported_missing():
    """Declared, and present only via inheritance — absent from the local folder
    but not actually missing."""
    declared = declared_state(REGISTRY["ZoneRequest"], [_zone(name="internet")])
    r = detect_object_drift(
        declared, [_actual(name="internet", folder="ngfw-shared", scope="prod-edge")])
    assert r.missing == () and r.is_clean


def test_inheritance_does_not_mask_a_locally_defined_rogue():
    r = detect_object_drift({}, [
        _actual(name="internet", folder="ngfw-shared", scope="prod-edge"),
        _actual(name="rogue", folder="prod-edge", scope="prod-edge"),
    ])
    assert [o.name for o in r.unexpected] == ["rogue"]
    assert len(r.inherited) == 1


def test_declared_state_reuses_the_kinds_own_tfvars_emitter():
    """So the drift comparison and what Terraform applies cannot disagree about
    what an object is supposed to look like — for ANY kind, not just zones."""
    from fwgitops.compiler import zone_tfvars
    z = _zone(protection_profile="best-practice", user_id=True)
    got = declared_state(REGISTRY["ZoneRequest"], [z])[("prod-edge", "dmz")]
    assert got == zone_tfvars([z])["zones"]["dmz"]


def test_declared_state_works_for_interfaces_too():
    """The gap this closes: the registry declared drift_engine="state" for
    InterfaceRequest while `declared_zone_state` only knew about zones."""
    from fwgitops.compiler import CompiledInterface, interface_tfvars
    i = CompiledInterface(folder="prod-edge", name="$eth-local", ip=["10.0.1.1/24"])
    got = declared_state(REGISTRY["InterfaceRequest"], [i])
    assert got[("prod-edge", "$eth-local")] == interface_tfvars([i])["interfaces"]["$eth-local"]


def test_every_state_drift_kind_can_produce_declared_state():
    """A kind declaring state-based drift with no working declared_state would
    be a registry claim the code does not keep — which is exactly what happened
    to InterfaceRequest."""
    from fwgitops.kinds import kinds_with_drift_engine
    for handler in kinds_with_drift_engine("state"):
        assert callable(handler.tfvars), f"{handler.kind} cannot produce declared state"
        assert handler.state_api_path, f"{handler.kind} has no snapshot source"


# ── state drift had never worked for anything but rules ───────────────────
def test_declared_state_handles_an_AGGREGATING_kind():
    """A RouteRequest is not an SCM object — routes collapse into a logical
    router, so `tfvars([one_route])` returns a router keyed by the ROUTER name,
    not the request id. Indexing by `name_of(obj)` raised
    `KeyError: 'REQ-2026-0803'` the moment routes reached the declared set.

    Calling tfvars per object would also compare a router holding ONE route
    against SCM's router holding all of them — drift that is not there.
    """
    import yaml

    from fwgitops.catalog import FolderHierarchy, RouterCatalog
    from fwgitops.drift import declared_state
    from fwgitops.intent import load_intent
    from fwgitops.kinds import REGISTRY, compile_any
    from fwgitops.resolve import EnvMap

    env = EnvMap.from_dict({"prod": {"folder": "prod-edge", "from_zone": "l", "to_zone": "i"}})
    routers = RouterCatalog.from_dict({"routers": {
        "prod-edge": {"default": {"vrfs": {"default": {"interfaces": ["$eth-local"]}}}}}})
    hier = FolderHierarchy.from_dict(
        {"folders": {"prod-edge": {"children": [], "targetable": True}}})

    def route(rid, dest):
        return load_intent({
            "apiVersion": "fw-intent/v1", "kind": "RouteRequest",
            "metadata": {"id": rid, "requester": "m@corp", "ticket": "J-1",
                         "justification": "x", "requested": "2026-08-08"},
            "spec": {"folder": "prod-edge", "destination": dest, "nexthop": "10.0.0.1"},
        }, env_map=env, router_catalog=routers, folder_hierarchy=hier)

    objs = [compile_any(route("R-1", "0.0.0.0/0"), env),
            compile_any(route("R-2", "10.9.0.0/16"), env)]
    state = declared_state(REGISTRY["RouteRequest"], objs)

    # ONE router, keyed by the SCM object's name — not two entries keyed by id.
    assert list(state) == [("prod-edge", "default")]
    vrf = state[("prod-edge", "default")]["vrf"][0]
    names = [r["name"] for r in vrf["routing_table"]["ip"]["static_route"]]
    assert names == ["R-1", "R-2"], "both routes must aggregate into the one router"


def test_a_nested_null_is_not_reported_as_modified():
    """`_flatten` does not descend into lists, so a router's `vrf` is compared
    whole — and the compiled form carries explicit nulls (`interface: None`)
    where SCM omits the key. Without normalising, an untouched router reported as
    `modified` on every run.

    "A None in the declaration means we did not ask for this" is already the
    contract for top-level fields; this is the same statement one level down.
    """
    from fwgitops.drift import ActualObject, detect_object_drift

    declared = {("prod-edge", "default"): {
        "vrf": [{"name": "default", "interface": None,
                 "routing_table": {"ip": {"static_route": [
                     {"name": "R-1", "destination": "0.0.0.0/0", "metric": 10,
                      "interface": None, "admin_dist": None}]}}}]}}
    actual = [ActualObject(
        kind="RouteRequest", folder="prod-edge", name="default", scope="prod-edge",
        fields={"vrf": [{"name": "default",
                         "routing_table": {"ip": {"static_route": [
                             {"name": "R-1", "destination": "0.0.0.0/0",
                              "metric": 10}]}}}]})]
    assert detect_object_drift(declared, actual).is_clean


def test_an_INHERITED_rule_is_not_drift():
    """A folder read returns everything that APPLIES to it, ancestors included.

    PAN-OS ships `All/default`, `All/Web-Security-Default`, `All/hip-default`;
    a snippet contributes `ngfw-shared/Auto-VPN-Default-Snippet`. None carries a
    gitops: tag, so all four look "added outside GitOps" — and on the first live
    run of the tag engine all six across two scopes were reported as drift, in a
    job whose warning IS the alert.

    They belong to whoever owns the ancestor. The state engine already drew this
    line; drawing it differently here would make the same rule drift or not
    depending on which engine looked.
    """
    from fwgitops.drift import ActualRule, detect_drift

    report = detect_drift([], [
        ActualRule(folder="All", name="default", tags=(), scope="prod-edge"),
        ActualRule(folder="ngfw-shared", name="Auto-VPN-Default-Snippet",
                   tags=(), scope="prod-edge"),
    ])
    assert report.is_clean, f"inherited rules must not read as drift: {report.summary()}"
    assert len(report.inherited) == 2
    assert "not checked — owned by an ancestor folder" in report.summary()


def test_a_rule_added_LOCALLY_without_a_tag_is_still_drift():
    """The inheritance skip must not become a blanket amnesty — a hand-added
    rule in the folder under inspection is exactly what this engine is for."""
    from fwgitops.drift import ActualRule, detect_drift

    report = detect_drift([], [
        ActualRule(folder="prod-edge", name="MANUAL-RULE", tags=(), scope="prod-edge"),
    ])
    assert not report.is_clean
    assert [r.name for r in report.unmanaged] == ["MANUAL-RULE"]


def test_a_snapshot_without_scope_treats_rules_as_LOCAL():
    """Fail toward reporting. An older snapshot with no `scope` field cannot
    prove a rule is inherited, and silently skipping it would hide real drift —
    the opposite of the mistake being fixed."""
    from fwgitops.drift import ActualRule, detect_drift

    report = detect_drift([], [ActualRule(folder="All", name="default", tags=())])
    assert not report.is_clean, "unknown provenance must be reported, not skipped"


def test_a_DUPLICATED_managed_rule_does_not_pass_as_clean():
    """The hole found by asking what `malformed` is for.

    Copy a managed rule in the SCM console and the copy inherits its tags —
    `gitops:managed` plus `gitops:req:REQ-...`. The original check asked only
    whether the TAG's request was declared, never whether the object's own name
    matched it. So the duplicate was not unmanaged, not malformed and not
    orphaned: it reported NO DRIFT, while being free to carry any contents at
    all — copy a narrow rule, widen the copy, and nothing says a word.

    Every managed rule is named after its request (`name = metadata.id`), so a
    managed object whose name differs from the request it claims did not come
    from this pipeline.
    """
    from fwgitops.compiler import CompiledChange, SecurityRule
    from fwgitops.drift import ActualRule, detect_drift

    rule = SecurityRule(name="REQ-2026-0725", folder="prod-edge",
                        from_zones=["local"], to_zones=["internet"], sources=[],
                        destinations=[], services=[], action="allow",
                        log_end=True, tags=[], profile_group=None,
                        negate_source=False, negate_destination=False)
    declared = [CompiledChange(address_objects=[], service_objects=[], rule=rule)]

    copy = ActualRule(folder="prod-edge", name="REQ-2026-0725-copy",
                      tags=("gitops:managed", "gitops:req:REQ-2026-0725"),
                      scope="prod-edge")

    report = detect_drift(declared, [copy])
    assert not report.is_clean, "a duplicated managed rule must not read as clean"
    assert [r.name for r in report.malformed] == ["REQ-2026-0725-copy"]


def test_the_GENUINE_managed_rule_is_still_clean():
    """The obvious regression: tightening the check must not flag the real one."""
    from fwgitops.compiler import CompiledChange, SecurityRule
    from fwgitops.drift import ActualRule, detect_drift

    rule = SecurityRule(name="REQ-2026-0725", folder="prod-edge",
                        from_zones=["local"], to_zones=["internet"], sources=[],
                        destinations=[], services=[], action="allow",
                        log_end=True, tags=[], profile_group=None,
                        negate_source=False, negate_destination=False)
    declared = [CompiledChange(address_objects=[], service_objects=[], rule=rule)]

    real = ActualRule(folder="prod-edge", name="REQ-2026-0725",
                      tags=("gitops:managed", "gitops:req:REQ-2026-0725"),
                      scope="prod-edge")
    assert detect_drift(declared, [real]).is_clean
