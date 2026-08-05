"""InterfaceRequest — intent kind #3 (ADR-0001, designed in ADR-0005).

CONFIGURES an existing interface rather than creating one: on the pilot tenant
the interfaces exist as folder-scope variables (`$eth-local`) with `layer3`
empty, and what an InterfaceRequest supplies is the addressing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.catalog import InterfaceCatalog, NameCatalog  # noqa: E402
from fwgitops.compiler import CompiledInterface, interface_tfvars  # noqa: E402
from fwgitops.compiler import CompileError  # noqa: E402
from fwgitops.intent import IntentError, load_intent  # noqa: E402
from fwgitops.kinds import REGISTRY, compile_any  # noqa: E402
from fwgitops.resolve import EnvMap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


#: `interface:` names a ROLE, not a port — the same interface is `$eth-local` at
#: folder scope and `ethernet1/4` at device scope, so no literal is right for
#: both. catalog/interfaces.yaml resolves it per scope.
def _ifcat():
    return InterfaceCatalog.from_dict({"interfaces": {
        "local": {"folder": "$eth-local", "devices": {"007955000894453": "ethernet1/4"}},
        "internet": {"folder": "$eth-internet", "devices": {"007955000894453": "ethernet1/3"}},
    }})


def _doc(**spec):
    base = {"environment": "prod", "interface": "local", "ip": ["10.20.0.1/24"]}
    base.update(spec)
    return {
        "apiVersion": "fw-intent/v1", "kind": "InterfaceRequest",
        "metadata": {"id": "IF-1", "requester": "m@corp", "ticket": "J-1",
                     "justification": "x", "requested": "2026-08-02"},
        "spec": base,
    }


def _env():
    return EnvMap.from_dict(
        {"prod": {"folder": "prod-edge", "from_zone": "local", "to_zone": "internet"}})


# ── schema ────────────────────────────────────────────────────────────────
def test_a_static_addressed_interface_loads():
    sp = load_intent(_doc(mtu=1500, comment="Internal"), interface_catalog=_ifcat()).spec
    assert sp.interface == "$eth-local"   # role `local` resolved at folder scope and sp.ip == ["10.20.0.1/24"]
    assert sp.mtu == 1500 and sp.comment == "Internal" and sp.dhcp is False


def test_a_dhcp_interface_loads():
    sp = load_intent(_doc(ip=[], dhcp=True), interface_catalog=_ifcat()).spec
    assert sp.dhcp is True and sp.ip == []


def test_exactly_one_addressing_mode_is_required():
    """The provider says "exactly one of dhcp_client, ip, and pppoe". Catching
    it here beats catching it at the device commit, where the message is worse
    and the change is already in a candidate config."""
    with pytest.raises(IntentError, match="exactly one"):
        load_intent(_doc(dhcp=True), interface_catalog=_ifcat())            # ip AND dhcp
    with pytest.raises(IntentError, match="addressing mode"):
        load_intent(_doc(ip=[]), interface_catalog=_ifcat())                 # neither


@pytest.mark.parametrize("bad,frag", [
    ({"ip": ["10.20.0.1"]}, "CIDR"),
    ({"ip": ["not-an-address/24"]}, "CIDR"),
    ({"ip": "10.0.0.1/24"}, "list of CIDR"),
    ({"mtu": 0}, "positive integer"),
    ({"mtu": "big"}, "positive integer"),
    ({"interface": ""}, "interface"),
    ({"interface": "nope"}, "unknown interface role"),
])
def test_bad_shapes_are_rejected(bad, frag):
    with pytest.raises(IntentError) as e:
        load_intent(_doc(**bad), interface_catalog=_ifcat())
    assert any(frag in str(p) for p in e.value.problems)


def test_the_management_profile_is_catalog_validated():
    cat = NameCatalog(kind="interface management profile",
                      names=frozenset({"auto-vpn-ping-profile"}))
    load_intent(_doc(management_profile="auto-vpn-ping-profile"),
                interface_catalog=_ifcat(), interface_profile_catalog=cat)
    with pytest.raises(IntentError) as e:
        load_intent(_doc(management_profile="typo"), interface_catalog=_ifcat(), interface_profile_catalog=cat)
    assert any("interface management profile 'typo'" in str(p) for p in e.value.problems)


# ── compile ───────────────────────────────────────────────────────────────
def test_compiles_to_the_provider_shape():
    """Addressing lives under `layer3`, and EXACTLY ONE of ip / dhcp_client is
    non-null — mirroring scm_ethernet_interface so a reader can diff it against
    `terraform providers schema` without translating."""
    c = compile_any(load_intent(_doc(mtu=1500), interface_catalog=_ifcat()), _env())
    got = interface_tfvars([c])["interfaces"]["$eth-local"]
    assert got["folder"] == "prod-edge"
    assert got["layer3"]["ip"] == [{"name": "10.20.0.1/24"}]
    assert got["layer3"]["dhcp_client"] is None
    assert got["layer3"]["mtu"] == 1500


def test_dhcp_compiles_to_dhcp_client_with_ip_null():
    c = compile_any(load_intent(_doc(ip=[], dhcp=True), interface_catalog=_ifcat()), _env())
    l3 = interface_tfvars([c])["interfaces"]["$eth-local"]["layer3"]
    assert l3["dhcp_client"] == {"enable": True} and l3["ip"] is None


def test_two_requests_for_the_same_interface_are_rejected():
    a = CompiledInterface(folder="f", name="$eth-local", ip=["10.0.0.1/24"])
    b = CompiledInterface(folder="f", name="$eth-local", ip=["10.0.0.2/24"])
    with pytest.raises(CompileError, match="duplicate interface key"):
        interface_tfvars([a, b])


# ── classify (ADR-0005 prerequisites 1 and 2) ─────────────────────────────
def _cur(addressing, name="$eth-local", folder="prod-edge"):
    return {(folder, name): {"name": name, "layer3": addressing}}


def test_addressing_an_empty_interface_is_high():
    """Every interface on the pilot tenant sits at `layer3: {}`, so the FIRST
    InterfaceRequest against any of them is the populating case — it puts the
    interface on a network."""
    from fwgitops.classify import classify_interface
    c = compile_any(load_intent(_doc(), interface_catalog=_ifcat()), _env())
    v = classify_interface(c, current=_cur({}))
    assert v.tier == "HIGH"
    assert "interface_becomes_addressed" in [x["check"] for x in v.checks_fired]


def test_changing_an_already_addressed_interface_is_not_the_same_act():
    from fwgitops.classify import classify_interface
    c = compile_any(load_intent(_doc(), interface_catalog=_ifcat()), _env())
    v = classify_interface(c, current=_cur({"ip": [{"name": "10.0.0.9/24"}]}))
    assert v.tier == "LOW" and v.checks_fired == ()


def test_dhcp_counts_as_existing_addressing():
    from fwgitops.classify import classify_interface
    c = compile_any(load_intent(_doc(), interface_catalog=_ifcat()), _env())
    v = classify_interface(c, current=_cur({"dhcp_client": {"enable": True}}))
    assert v.checks_fired == ()


def test_without_a_snapshot_the_check_is_skipped_not_guessed():
    from fwgitops.classify import classify_interface
    assert classify_interface(compile_any(load_intent(_doc(), interface_catalog=_ifcat()), _env())).checks_fired == ()


def test_an_interface_scoped_to_a_folder_with_children_is_high():
    """ADR-0005's blast-radius control, on the kind it was built for. Pointing an
    env at ngfw-shared reaches prod-edge AND GitOps."""
    from fwgitops.catalog import FolderHierarchy
    from fwgitops.classify import classify_interface
    h = FolderHierarchy.from_dict({"folders": {"ngfw-shared": {"children": ["prod-edge", "GitOps"]}}})
    c = CompiledInterface(folder="ngfw-shared", name="$eth-local", ip=["10.0.0.1/24"])
    v = classify_interface(c, hierarchy=h)
    assert v.tier == "HIGH"
    assert "folder_with_children" in [x["check"] for x in v.checks_fired]


# ── registry + contract ───────────────────────────────────────────────────
def test_the_kind_is_registered_once_and_drives_everything():
    h = REGISTRY["InterfaceRequest"]
    assert h.tfvars_filename == "interfaces.auto.tfvars.json"
    assert h.report_prefix == "interface/"
    assert h.drift_engine == "state"          # scm_ethernet_interface has no tag
    assert h.state_api_path == "/config/network/v1/ethernet-interfaces"
    assert h.has_evidence is False


def test_the_repos_root_declares_every_path_the_compiler_emits():
    """HOLE 3 at depth: `layer3` is nested, and Terraform discards undeclared
    attributes at any level, silently."""
    from fwgitops.tfcontract import check_contract, check_object_attributes
    c = CompiledInterface(folder="prod-edge", name="$eth-local", ip=["10.0.0.1/24"],
                          mtu=1500, comment="x", management_profile="auto-vpn-ping-profile")
    payload = interface_tfvars([c])
    root = REPO_ROOT / "terraform" / "prod-edge"
    assert check_contract(root, sorted(payload)) == []
    assert check_object_attributes(root, "interfaces", payload["interfaces"]) == []


# ── the interface catalog: a role, not a port ─────────────────────────────
def test_the_same_role_resolves_differently_per_scope():
    """The whole reason this catalog exists. `$eth-local` and `ethernet1/4` are
    ONE object seen at two scopes (ADR-0005), so no literal is correct for both
    — an intent hardcoding either is wrong the moment its target changes."""
    cat = _ifcat()
    assert cat.resolve("local", device=None) == "$eth-local"
    assert cat.resolve("local", device="007955000894453") == "ethernet1/4"


def test_a_role_is_resolved_against_the_intents_actual_target():
    from fwgitops.catalog import FolderHierarchy
    import yaml
    h = FolderHierarchy.from_dict(yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))

    at_folder = load_intent(_doc(environment=None, folder="prod-edge"),
                            interface_catalog=_ifcat(), folder_hierarchy=h)
    at_device = load_intent(_doc(environment=None, device="007955000894453"),
                            interface_catalog=_ifcat(), folder_hierarchy=h)
    assert at_folder.spec.interface == "$eth-local"
    assert at_device.spec.interface == "ethernet1/4"


def test_an_unknown_role_is_rejected_with_the_known_ones():
    with pytest.raises(IntentError) as e:
        load_intent(_doc(interface="etherent1/4"), interface_catalog=_ifcat())
    msg = " ".join(str(p) for p in e.value.problems)
    assert "unknown interface role" in msg and "local" in msg


def test_a_role_with_no_mapping_for_that_firewall_is_rejected():
    """Fail closed. Guessing a port is how an intent lands on the wrong wire —
    and on AWS the physical name depends on which ENI index exists at all.

    SYNTHETIC hierarchy on purpose. This asserts a property of the LOADER, so it
    must not depend on the shipped catalog happening to contain a targetable
    firewall that some role does not map. It did until 2026-08-05, when
    007955000893662 vanished from SCM and was marked non-targetable — at which
    point the intent was rejected one step EARLIER, for targetability, and this
    test passed for the wrong reason right up until it failed.
    """
    from fwgitops.catalog import FolderHierarchy
    h = FolderHierarchy.from_dict({"folders": {"prod-edge": {
        "children": [], "targetable": True,
        "devices": {"007955000899999": {"display_name": "fw-unmapped", "model": "PA-VM",
                                        "targetable": True}}}}})
    with pytest.raises(IntentError, match="no mapping for firewall"):
        load_intent(_doc(environment=None, device="007955000899999"),
                    interface_catalog=_ifcat(), folder_hierarchy=h)


def test_without_the_catalog_the_field_is_unusable_not_unchecked():
    """The dangerous failure would be falling back to the literal — that is
    exactly the hardcoded-port problem the catalog removes."""
    with pytest.raises(IntentError, match="Refusing to guess a physical port"):
        load_intent(_doc())


def test_the_shipped_catalog_covers_every_targetable_firewall():
    """A firewall that is targetable but has no interface mapping can be named
    by an intent that then fails at load — catch the gap here instead."""
    import yaml

    from fwgitops.catalog import FolderHierarchy, InterfaceCatalog
    cat = InterfaceCatalog.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "interfaces.yaml").read_text()))
    h = FolderHierarchy.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))
    for serial in h.targetable_device_serials():
        # UNIVERSAL roles only. A role marked `site_specific` is not expected on
        # every firewall (a DMZ port is one site's wiring), and the test below
        # asserts that such a role still fails CLOSED for an unmapped firewall.
        # Skipping them here narrows what is expected; it does not weaken what
        # is enforced.
        for role in cat.universal_roles():
            assert cat.resolve(role, device=serial), f"{role} unmapped for {serial}"


def test_a_site_specific_role_still_fails_closed_for_an_unmapped_firewall():
    """`site_specific` changes what the coverage test EXPECTS, never what the
    loader ENFORCES. Without this, marking a role site-specific would look like
    a way to opt out of the guard rather than to describe the topology."""
    import yaml

    from fwgitops.catalog import CatalogError, FolderHierarchy, InterfaceCatalog
    cat = InterfaceCatalog.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "interfaces.yaml").read_text()))
    h = FolderHierarchy.from_dict(
        yaml.safe_load((REPO_ROOT / "catalog" / "folders.yaml").read_text()))

    site_roles = [r for r in cat.roles() if r not in cat.universal_roles()]
    assert site_roles, "expected at least one site-specific role (dmz) in the shipped catalog"

    # The ENFORCEMENT is asserted against a synthetic firewall, not against
    # whichever serial the shipped catalog currently happens to leave unmapped.
    # It used to be 007955000893662; that firewall left SCM on 2026-08-05 and is
    # now non-targetable, so the shipped catalog maps every targetable firewall
    # for every role. That is a fine state of the world, and it must not silently
    # turn this test into a no-op.
    for role in site_roles:
        with pytest.raises(CatalogError, match="no mapping for firewall"):
            cat.resolve(role, device="007955000899999")

    # Whether the shipped marking still MEANS anything is only decidable with
    # more than one firewall. With exactly one, every role covers it and
    # "site-specific" is indistinguishable from universal by coverage — the
    # marking is a statement about the NEXT firewall, which no assertion here can
    # see. Asserting it anyway would fail the moment the estate shrank to one,
    # which is what happened when 007955000893662 left SCM on 2026-08-05.
    #
    # So the check is CONDITIONAL rather than deleted: it goes quiet on a
    # one-firewall estate and comes back the moment there is something to
    # compare.
    universal_cover = max(
        (len(cat.device_names.get(r, {})) for r in cat.universal_roles()), default=0)
    if universal_cover > 1:
        for role in site_roles:
            assert len(cat.device_names.get(role, {})) < universal_cover, (
                f"{role} is marked site_specific but covers as many firewalls as a "
                f"universal role — the marking is either wrong or now pointless")
