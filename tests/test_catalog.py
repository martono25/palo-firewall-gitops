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
