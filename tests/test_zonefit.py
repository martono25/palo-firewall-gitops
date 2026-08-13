"""Zone resolution must agree with the firewall by CONSTRUCTION, not by luck.

The pilot's own numbers are used throughout: interfaces on 10.100.1/2/3.0/24,
a default route via 10.100.2.1, and rules addressing 10.20.x.x. That
combination is what proved on 2026-08-13 that every declared rule is placed on a
zone pair its traffic does not use.
"""

from __future__ import annotations

from pathlib import Path

from fwgitops.zonefit import RoutingView, check_rule

# The pilot, exactly as its intent declares it.
PILOT = dict(
    interfaces=[("local", ["10.100.3.125/24"]),
                ("internet", ["10.100.2.142/24"]),
                ("dmz", ["10.100.1.110/24"])],
    routes=[("0.0.0.0/0", "10.100.2.1")],
    zone_of_role={"local": "local", "internet": "internet", "dmz": "dmz"},
)


def _view():
    return RoutingView.build(**PILOT)


def test_a_connected_subnet_resolves_to_its_own_zone():
    v = _view()
    assert v.zone_for("10.100.3.50/32") == "local"
    assert v.zone_for("10.100.1.7/32") == "dmz"
    assert v.zone_for("10.100.2.9/32") == "internet"


def test_the_default_route_carries_everything_else_to_the_INTERNET_zone():
    """The finding. 10.20.x is on no interface of this firewall, so it matches
    only 0.0.0.0/0, whose nexthop 10.100.2.1 sits on the internet interface."""
    v = _view()
    assert v.zone_for("10.20.1.55/32") == "internet"
    assert v.zone_for("10.20.20.10/32") == "internet"


def test_longest_prefix_wins_even_when_the_longer_one_is_found_LAST():
    """The version of this test that shipped first was worthless.

    It asserted a connected /24 beats 0.0.0.0/0 — which passes under
    "first match wins" too, because connected routes are appended before static
    ones. It pinned LIST ORDER and read like it pinned prefix length. A mutation
    replacing the longest-prefix comparison with `if best is None` left it
    green.

    So the more specific route is deliberately the one added LAST here: a static
    /24 over a connected /16. Only a real longest-prefix match gets this right.
    """
    v = RoutingView.build(
        interfaces=[("local", ["10.100.0.1/16"]),      # broad, found FIRST
                    ("dmz", ["10.200.0.1/24"])],
        routes=[("10.100.5.0/24", "10.200.0.9")],      # specific, found LAST
        zone_of_role={"local": "local", "dmz": "dmz"},
    )
    assert v.zone_for("10.100.5.20/32") == "dmz", (
        "the /24 static route is more specific than the /16 connected one and "
        "must win, whichever was discovered first")
    assert v.zone_for("10.100.9.20/32") == "local", "and the /16 still covers the rest"


def test_a_connected_route_still_beats_the_default_route():
    v = _view()
    assert v.zone_for("10.100.3.125/32") == "local"


def test_a_static_route_egresses_via_the_interface_HOLDING_ITS_NEXTHOP():
    """That is how the device resolves it. Deriving it any other way would make
    this agree with the firewall by accident."""
    v = RoutingView.build(
        interfaces=[("local", ["10.100.3.1/24"]), ("dmz", ["10.100.1.1/24"])],
        routes=[("192.168.0.0/16", "10.100.1.9")],   # nexthop is on the dmz side
        zone_of_role={"local": "local", "dmz": "dmz"},
    )
    assert v.zone_for("192.168.4.4/32") == "dmz"


def test_an_UNRESOLVABLE_nexthop_yields_no_route_rather_than_a_guess():
    """A nexthop on no connected subnet is exactly where inventing an answer
    would make this agree with the firewall by luck."""
    v = RoutingView.build(
        interfaces=[("local", ["10.100.3.1/24"])],
        routes=[("192.168.0.0/16", "172.31.9.9")],   # nowhere near a connected net
        zone_of_role={"local": "local"},
    )
    assert v.zone_for("192.168.4.4/32") is None


def test_an_address_nothing_routes_returns_None_not_a_default():
    v = RoutingView.build(
        interfaces=[("local", ["10.100.3.1/24"])],
        routes=[],
        zone_of_role={"local": "local"},
    )
    assert v.zone_for("8.8.8.8/32") is None


# ── the check itself ────────────────────────────────────────────────────────

def test_the_pilots_real_rule_shape_is_reported_as_UNMATCHABLE():
    """REQ-2026-0809 as it stands: collector -> web host, placed local ->
    internet. Both addresses route out of `internet`, so the source side is
    wrong and the rule cannot match."""
    bad = check_rule(name="REQ-2026-0809",
                     sources=["10.20.20.10/32"], destinations=["10.20.1.55/32"],
                     from_zone="local", to_zone="internet", view=_view())

    assert [m.side for m in bad] == ["source"], (
        "the destination genuinely routes to internet; only the source is wrong")
    assert bad[0].routed_zone == "internet"
    assert "cannot match this traffic" in bad[0].reason


def test_a_rule_whose_addresses_DO_route_to_its_zones_is_clean():
    """The check has to be able to pass, or it is just noise."""
    assert check_rule(name="R", sources=["10.100.3.50/32"],
                      destinations=["10.20.9.9/32"],
                      from_zone="local", to_zone="internet", view=_view()) == []


def test_an_address_nothing_routes_is_reported_DIFFERENTLY_from_a_wrong_zone():
    """"I cannot tell" and "this is wrong" are different findings, and merging
    them would let an unroutable address read as a confident verdict."""
    v = RoutingView.build(interfaces=[("local", ["10.100.3.1/24"])], routes=[],
                          zone_of_role={"local": "local"})
    bad = check_rule(name="R", sources=["8.8.8.8/32"], destinations=["10.100.3.4/32"],
                     from_zone="local", to_zone="local", view=v)
    assert len(bad) == 1 and bad[0].routed_zone is None
    assert "matches no route this repository declares" in bad[0].reason


def test_an_unparseable_address_does_not_crash_the_check():
    v = _view()
    bad = check_rule(name="R", sources=["not-an-ip"], destinations=["10.20.1.1/32"],
                     from_zone="local", to_zone="internet", view=v)
    assert [m.address for m in bad] == ["not-an-ip"]
    assert bad[0].routed_zone is None


def test_the_module_records_that_PILOT_findings_are_expected():
    """Decided 2026-08-13: the pilot provisions whatever a valid request asks
    for, and its addresses are notional. Every rule here therefore reports a
    mismatch, and that is the environment rather than a defect.

    Pinned because the finding is alarming out of context — "every rule is
    unmatchable" invites someone to rewrite the intents to silence it, which
    would change a working pilot to satisfy a report nobody asked to act on.
    """
    import re

    src = (Path(__file__).resolve().parents[1] / "src" / "fwgitops"
           / "zonefit.py").read_text()
    # Whitespace collapsed: the docstring is hard-wrapped, so a phrase worth
    # asserting on is usually split across two lines. Asserting against the raw
    # text tests the line wrapping, which nobody cares about — and which is
    # exactly how this test failed first time round.
    flat = re.sub(r"\s+", " ", src)
    assert "EXPECTED AND NOT DEFECTS" in flat
    assert 'Do not "fix" the intents to satisfy this module' in flat
