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
