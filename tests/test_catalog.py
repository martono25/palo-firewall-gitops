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
    c = app({"environment": "prod", "zone": "local",
             "addresses": ["10.20.1.0/24"], "fqdns": ["a.internal"]})
    a = c.resolve("web")
    assert a == AppDef("prod", "local", ("10.20.1.0/24",), ("a.internal",))


def test_app_addresses_optional_if_fqdns_present():
    c = app({"environment": "prod", "zone": "local", "fqdns": ["a.internal"]})
    assert c.resolve("web").addresses == ()


def test_unknown_app_raises():
    c = app({"environment": "prod", "zone": "local", "addresses": ["10.0.0.0/8"]})
    with pytest.raises(CatalogError, match="unknown app"):
        c.resolve("nope")


@pytest.mark.parametrize("bad", [
    {"folder": "f", "zone": "z", "addresses": ["10.0.0.0/8"]},              # missing environment
    {"environment": "p", "zone": "z"},                        # no address or fqdn
    {"environment": "p", "zone": "z", "addresses": ["nope"]}, # bad CIDR
    {"environment": "p", "zone": "z", "addresses": ["10.20.1.5/24"]},  # host bits
    {"environment": "p", "zone": "z", "addresses": "not-a-list"},      # wrong shape
])
def test_malformed_app_fails_closed(bad):
    with pytest.raises(CatalogError):
        app(bad)


# ── NameCatalog (ADR-0003 reference allowlists) ────────────────────────────
from fwgitops.catalog import NameCatalog  # noqa: E402


def ncat(entries, **kw):
    return NameCatalog.from_dict(entries, kind="App-ID", key="applications", **kw)


def test_namecatalog_list_form():
    c = ncat({"applications": ["ssl", "web-browsing"]})
    c.validate("ssl")  # no raise
    with pytest.raises(CatalogError):
        c.validate("nope")


def test_namecatalog_mapping_form_ignores_metadata():
    c = ncat({"applications": {"ssl": {"desc": "x"}, "dns": None}})
    c.validate("dns")
    assert c.names == frozenset({"ssl", "dns"})


def test_namecatalog_bare_list_and_map():
    NameCatalog.from_dict(["a", "b"], kind="k", key="applications").validate("a")
    NameCatalog.from_dict({"a": None}, kind="k", key="applications").validate("a")


def test_namecatalog_always_valid_not_listed():
    c = ncat({"applications": ["ssl"]}, always_valid=frozenset({"any"}))
    c.validate("any")  # accepted though not in names
    assert "any" not in c.names


def test_namecatalog_unknown_lists_known():
    with pytest.raises(CatalogError) as ei:
        ncat({"applications": ["ssl"]}).validate("bogus")
    assert "ssl" in str(ei.value) and "bogus" in str(ei.value)


def test_namecatalog_empty_reports_empty():
    with pytest.raises(CatalogError) as ei:
        ncat({"applications": []}).validate("x")
    assert "catalog is empty" in str(ei.value)


@pytest.mark.parametrize("bad", [
    {"applications": {"": None}},   # blank name
    {"applications": [123]},        # non-string
    {"applications": 5},            # not a mapping/list
])
def test_namecatalog_malformed_fails_closed(bad):
    with pytest.raises(CatalogError):
        ncat(bad)


def test_an_app_declaring_a_folder_is_REJECTED():
    """`folder:` was declared on every shipped app and NEVER READ — `_target()`
    has always taken the folder from `env_map.resolve(environment)`. An app whose
    folder contradicted its environment loaded fine and the contradiction did
    nothing.

    Deleting the field without rejecting it would leave those files looking
    correct while quietly changing nothing, which is the same silent-drop the
    `expires` retirement had to guard against.
    """
    with pytest.raises(CatalogError, match="an app does not choose the folder"):
        app({"environment": "prod", "folder": "prod-edge", "zone": "local",
             "addresses": ["10.20.1.0/24"]})


def test_the_shipped_app_catalog_declares_no_folders():
    from pathlib import Path as _Path

    import yaml
    root = _Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "catalog" / "apps.yaml").read_text())
    offenders = [n for n, spec in (raw.get("apps") or {}).items()
                 if isinstance(spec, dict) and "folder" in spec]
    assert not offenders, f"apps still declaring a folder: {offenders}"


def test_the_old_hostname_key_is_REJECTED():
    """`hostname:` was parsed by NOTHING — a decorative field in a file that
    otherwise drives behaviour — and it never held the hostname (that is
    DHCP-assigned, `ip-10-100-0-51`, and undeclared). Renaming it silently would
    leave the old key looking meaningful while doing nothing, which is the state
    it was already in."""
    from fwgitops.catalog import FolderHierarchy
    with pytest.raises(CatalogError, match="renamed to `display_name`"):
        FolderHierarchy.from_dict({"folders": {"prod-edge": {
            "children": [], "targetable": True,
            "devices": {"007955000901881": {"hostname": "fw-a", "targetable": True}}}}})
