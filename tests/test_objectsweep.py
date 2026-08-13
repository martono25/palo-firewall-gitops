"""Address and service objects leave Terraform's hands (ADR-0010).

The failure being designed around was measured on the pilot on 2026-08-13:
widening a rule's destination planned an in-place rule update plus a destroy of
the address the old value no longer needed, ran the destroy FIRST, and SCM
refused with 409 NON_ZERO_REFS because the rule still pointed at it.

These tests cover the two things that make a sweep safe to point at a live
tenant: it must delete only what this platform minted, and only what nothing is
using. Both failure directions are asserted, because only one of them is loud.
"""

from __future__ import annotations

import pytest

from fwgitops.objectsweep import (
    KIND_PATHS,
    ensure_objects,
    existing_objects,
    is_ours,
    plan_objects,
    referenced_names,
    sweep_objects,
)
from fwgitops.tags import object_name

SCOPE = {"folder": "prod-edge"}

# REAL object names from the live tenant, verified 2026-08-13. Using the real
# ones makes this a regression test for the incident rather than a test of an
# invented convention.
ADDR_HOST = "addr-a102bfc799"      # 10.20.1.55/32 — the one that refused to delete
ADDR_WIDE = "addr-5cbf6964f2"      # 10.20.0.0/16  — the orphan the failed apply left
ADDR_TIER = "addr-85c1076cfe"      # 10.20.1.0/24  — shared by three rules
SVC_DNS = "svc-df1c6fc644"         # udp/53


class FakeSession:
    """Canned GETs, recorded writes."""

    def __init__(self, objects=None, referrers=None, fail_on=None):
        self._objects = objects or {}
        self._referrers = referrers or {}
        self._fail_on = fail_on
        self.writes = []

    def request(self, method, path, params=None, body=None):
        if self._fail_on and self._fail_on in path and method == "GET":
            raise RuntimeError(f"SCM read failed for {path}")
        if method == "GET":
            if path in self._objects:
                return {"data": self._objects[path]}
            return {"data": self._referrers.get(path, [])}
        self.writes.append((method, path, body))
        return {}


def _addr(value):
    return {"name": object_name("address", value), "id": f"id-{value}",
            "ip_netmask": value}


# ── Ownership is PROVEN, not assumed ────────────────────────────────────────

def test_ownership_is_proven_by_the_name_being_the_hash_of_the_value():
    """Names are content-addressed, so an object is ours exactly when its name
    equals the name its own value hashes to. Verified here against the real
    names the live tenant reported."""
    assert is_ours("address", ADDR_HOST, "10.20.1.55/32")
    assert is_ours("address", ADDR_WIDE, "10.20.0.0/16")
    assert is_ours("address", ADDR_TIER, "10.20.1.0/24")
    assert is_ours("service", SVC_DNS, "udp/53")


def test_an_object_that_IMITATES_our_naming_is_not_ours():
    """A tag can only be recognised by a name prefix, which anyone can type. A
    hash cannot be. An object named like ours whose value does not hash to that
    name was not minted here and must never be swept."""
    assert not is_ours("address", ADDR_HOST, "10.99.99.99/32")
    assert not is_ours("address", "addr-deadbeef00", "10.20.1.55/32")


def test_an_object_whose_value_SCM_does_not_report_is_not_ours():
    """Unprovable means not ours — the fail-safe direction, since the
    consequence is leaving an inert object alone."""
    assert not is_ours("address", ADDR_HOST, None)
    assert not is_ours("address", ADDR_HOST, "")


# ── The sweep ───────────────────────────────────────────────────────────────

def test_sweep_deletes_only_what_is_ours_AND_unreferenced():
    session = FakeSession(
        objects={KIND_PATHS["address"]: [
            _addr("10.20.1.55/32"),                       # ours, unreferenced
            _addr("10.20.1.0/24"),                        # ours, referenced
            {"name": "corp-dns", "id": "id-x",            # foreign, unreferenced
             "ip_netmask": "10.0.0.53/32"},
        ]},
        referrers={"/config/security/v1/security-rules": [
            {"name": "R1", "source": ["addr-85c1076cfe"], "destination": ["any"]},
        ]},
    )
    plan = sweep_objects(session, "address", SCOPE, wanted=[])

    assert plan.unreferenced == [ADDR_HOST]
    assert plan.referenced == [ADDR_TIER]
    assert plan.foreign == 1
    assert session.writes == [("DELETE", f"{KIND_PATHS['address']}/id-10.20.1.55/32", None)]


def test_sweep_never_deletes_a_foreign_object_even_when_unreferenced():
    """Somebody else's object is not ours to tidy, whatever references it has."""
    session = FakeSession(
        objects={KIND_PATHS["address"]: [
            {"name": "corp-dns", "id": "id-x", "ip_netmask": "10.0.0.53/32"},
        ]},
        referrers={},
    )
    plan = sweep_objects(session, "address", SCOPE, wanted=[])
    assert plan.unreferenced == []
    assert plan.foreign == 1
    assert session.writes == []


def test_a_reference_ANYWHERE_in_a_referring_object_protects_it():
    """References are found by walking the whole document, not by reading field
    names we guessed. This one is nested somewhere a field-name reader would
    miss entirely — and missing it deletes an object a rule is using."""
    session = FakeSession(
        objects={KIND_PATHS["address"]: [_addr("10.20.1.55/32")]},
        referrers={"/config/objects/v1/address-groups": [
            {"name": "G1", "static": {"members": {"nested": [ADDR_HOST]}}},
        ]},
    )
    plan = sweep_objects(session, "address", SCOPE, wanted=[])
    assert plan.unreferenced == []
    assert plan.referenced == [ADDR_HOST]
    assert session.writes == []


def test_a_WANTED_object_survives_the_window_between_ensure_and_apply():
    """ensure_objects creates it, then the apply that references it has not run
    yet. Nothing references it at that instant, and sweeping it there would
    delete the object the imminent apply is about to point a rule at."""
    session = FakeSession(
        objects={KIND_PATHS["address"]: [_addr("10.20.0.0/16")]},
        referrers={},
    )
    plan = sweep_objects(session, "address", SCOPE, wanted=[ADDR_WIDE])
    assert plan.unreferenced == []
    assert session.writes == []


def test_a_failed_reference_read_RAISES_so_the_caller_sweeps_nothing():
    """A partial reference set makes a referenced object look unreferenced, and
    deleting one of those is the 409 this design exists to avoid — except done
    deliberately, which is worse. The read must fail loudly."""
    session = FakeSession(
        objects={KIND_PATHS["address"]: [_addr("10.20.1.55/32")]},
        fail_on="/config/objects/v1/address-groups",
    )
    with pytest.raises(RuntimeError):
        sweep_objects(session, "address", SCOPE, wanted=[])
    assert session.writes == []


# ── ensure ──────────────────────────────────────────────────────────────────

def test_ensure_creates_only_what_is_missing_and_never_edits():
    """An object's value defines its name, so one that exists under the right
    name already holds the right value. Editing could only ever be wrong."""
    session = FakeSession(
        objects={KIND_PATHS["address"]: [_addr("10.20.1.0/24")]},
    )
    wanted = {
        ADDR_TIER: {"ip_netmask": "10.20.1.0/24"},   # present
        ADDR_HOST: {"ip_netmask": "10.20.1.55/32"},  # missing
    }
    plan = ensure_objects(session, "address", SCOPE, wanted)

    assert plan.missing == [ADDR_HOST]
    assert [w[0] for w in session.writes] == ["POST"]
    assert session.writes[0][2] == {"folder": "prod-edge", "name": ADDR_HOST,
                                    "ip_netmask": "10.20.1.55/32"}


def test_ensure_is_idempotent():
    session = FakeSession(objects={KIND_PATHS["address"]: [_addr("10.20.1.0/24")]})
    wanted = {ADDR_TIER: {"ip_netmask": "10.20.1.0/24"}}
    assert ensure_objects(session, "address", SCOPE, wanted).missing == []
    assert session.writes == []


def test_services_round_trip_through_the_protocol_shape_SCM_reports():
    """SCM reports a service's port nested under its protocol, and the compiler
    names it from `protocol/port`. If those two disagree the object can never be
    proven ours, so it would never be swept — silently, and forever."""
    session = FakeSession(
        objects={KIND_PATHS["service"]: [
            {"name": SVC_DNS, "id": "id-dns",
             "protocol": {"udp": {"port": "53"}}},
        ]},
        referrers={},
    )
    present = existing_objects(session, "service", SCOPE)
    assert present[SVC_DNS]["value"] == "udp/53"

    plan = plan_objects("service", wanted=[], present=present, used=set())
    assert plan.unreferenced == [SVC_DNS], "a service must be provably ours"
    assert plan.foreign == 0


def test_referenced_names_reads_every_referrer_collection():
    """A collection nobody reads is a reference nobody sees."""
    from fwgitops.objectsweep import REFERRER_PATHS

    for kind, paths in REFERRER_PATHS.items():
        session = FakeSession(referrers={p: [{"n": f"seen-{i}"}]
                                         for i, p in enumerate(paths)})
        used = referenced_names(session, kind, SCOPE)
        for i in range(len(paths)):
            assert f"seen-{i}" in used, f"{kind} must read all its referrers"


def test_referrer_paths_are_ONLY_paths_the_tag_sweep_already_proves():
    """A referrer path that does not exist fails one of two ways, both bad.

    404 makes the sweep raise and never run — which is what a
    `/config/nat/v1/nat-rules` path, invented from the shape of the others, did
    on the first live run. Or worse, a path that returns something unexpected
    silently contributes no references and an object in use gets deleted.

    So this set may not exceed the one the tag sweep exercises on every apply.
    Widening it means confirming the path against the SCM API reference first,
    which is the step that was skipped.
    """
    from fwgitops.objectsweep import REFERRER_PATHS
    from fwgitops.tagsweep import REFERRER_PATHS as PROVEN

    unproven = {p for paths in REFERRER_PATHS.values() for p in paths} - set(PROVEN)
    assert not unproven, (
        f"these referrer paths are not exercised by the tag sweep, so nothing "
        f"proves they exist: {sorted(unproven)}")


def test_an_object_collection_is_NOT_a_referrer_for_its_own_kind():
    """The bug this nearly shipped with.

    The tag sweep reads `/config/objects/v1/addresses`, because an address
    CARRIES tags. Reusing that set for addresses means the addresses collection
    — which contains the address objects themselves — gets walked for
    references, so every address's own name turns up and every address looks
    referenced. Nothing is ever swept, silently and forever, in a way
    indistinguishable from a tenant with no garbage.
    """
    from fwgitops.objectsweep import KIND_PATHS, REFERRER_PATHS

    for kind, paths in REFERRER_PATHS.items():
        assert KIND_PATHS[kind] not in paths, (
            f"{kind} objects are being read as referrers of themselves, so "
            f"every one of them will look referenced and none will ever be swept")
