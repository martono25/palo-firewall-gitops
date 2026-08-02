"""Tests for tag-based drift detection."""

from __future__ import annotations

from fwgitops.drift import ActualRule, detect_drift
from fwgitops.tags import MANAGED_TAG, Section, managed_tags

from test_classify import _change


def mtags(req_id):
    return tuple(managed_tags(req_id=req_id, section=Section.SPECIFIC_ALLOW, ticket="T-1", expires=None))


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
