"""Tests for the service catalog (Phase 2 friendly service names)."""

from __future__ import annotations

import pytest

from fwgitops.catalog import CatalogError, ServiceCatalog, ServiceDef


def cat(services):
    return ServiceCatalog.from_dict({"services": services})


def test_resolves_known_name():
    assert cat({"https": {"protocol": "tcp", "port": "443"}}).resolve("https") == ServiceDef("tcp", "443")


def test_accepts_bare_map_without_services_key():
    c = ServiceCatalog.from_dict({"dns": {"protocol": "udp", "port": "53"}})
    assert c.resolve("dns") == ServiceDef("udp", "53")


def test_int_port_is_coerced_to_string():
    assert cat({"https": {"protocol": "tcp", "port": 443}}).resolve("https").port == "443"


def test_port_range_ok():
    assert cat({"eph": {"protocol": "tcp", "port": "1024-65535"}}).resolve("eph").port == "1024-65535"


def test_unknown_name_raises():
    with pytest.raises(CatalogError, match="unknown service"):
        cat({"https": {"protocol": "tcp", "port": "443"}}).resolve("ftp")


@pytest.mark.parametrize("bad", [
    {"x": {"protocol": "icmp", "port": "0"}},       # bad protocol
    {"x": {"protocol": "tcp", "port": "70000"}},    # out of range
    {"x": {"protocol": "tcp", "port": "abc"}},      # not a number
    {"x": {"protocol": "tcp", "port": "500-100"}},  # inverted range
    {"x": "not-a-mapping"},                          # wrong shape
])
def test_malformed_entry_fails_closed(bad):
    with pytest.raises(CatalogError):
        cat(bad)


def test_all_problems_reported_together():
    with pytest.raises(CatalogError) as ei:
        cat({"a": {"protocol": "x", "port": "1"}, "b": {"protocol": "tcp", "port": "z"}})
    msg = str(ei.value)
    assert "a.protocol" in msg and "b.port" in msg


def test_non_mapping_catalog_rejected():
    with pytest.raises(CatalogError, match="must be a mapping"):
        ServiceCatalog.from_dict(["not", "a", "map"])


# ── App catalog ────────────────────────────────────────────────────────────
from fwgitops.catalog import AppCatalog, AppDef  # noqa: E402


def app(spec):
    return AppCatalog.from_dict({"apps": {"web": spec}})


def test_app_resolves_addresses_and_zone():
    c = app({"environment": "prod", "folder": "prod-edge", "zone": "local",
             "addresses": ["10.20.1.0/24"], "fqdns": ["a.internal"]})
    a = c.resolve("web")
    assert a == AppDef("prod", "prod-edge", "local", ("10.20.1.0/24",), ("a.internal",))


def test_app_addresses_optional_if_fqdns_present():
    c = app({"environment": "prod", "folder": "f", "zone": "local", "fqdns": ["a.internal"]})
    assert c.resolve("web").addresses == ()


def test_unknown_app_raises():
    c = app({"environment": "prod", "folder": "f", "zone": "local", "addresses": ["10.0.0.0/8"]})
    with pytest.raises(CatalogError, match="unknown app"):
        c.resolve("nope")


@pytest.mark.parametrize("bad", [
    {"folder": "f", "zone": "z", "addresses": ["10.0.0.0/8"]},              # missing environment
    {"environment": "p", "folder": "f", "zone": "z"},                        # no address or fqdn
    {"environment": "p", "folder": "f", "zone": "z", "addresses": ["nope"]}, # bad CIDR
    {"environment": "p", "folder": "f", "zone": "z", "addresses": ["10.20.1.5/24"]},  # host bits
    {"environment": "p", "folder": "f", "zone": "z", "addresses": "not-a-list"},      # wrong shape
])
def test_malformed_app_fails_closed(bad):
    with pytest.raises(CatalogError):
        app(bad)
