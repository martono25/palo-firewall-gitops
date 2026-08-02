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

from fwgitops.catalog import NameCatalog  # noqa: E402
from fwgitops.compiler import CompiledInterface, interface_tfvars  # noqa: E402
from fwgitops.compiler import CompileError  # noqa: E402
from fwgitops.intent import IntentError, load_intent  # noqa: E402
from fwgitops.kinds import REGISTRY, compile_any  # noqa: E402
from fwgitops.resolve import EnvMap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _doc(**spec):
    base = {"environment": "prod", "interface": "$eth-local", "ip": ["10.20.0.1/24"]}
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
    sp = load_intent(_doc(mtu=1500, comment="Internal")).spec
    assert sp.interface == "$eth-local" and sp.ip == ["10.20.0.1/24"]
    assert sp.mtu == 1500 and sp.comment == "Internal" and sp.dhcp is False


def test_a_dhcp_interface_loads():
    sp = load_intent(_doc(ip=[], dhcp=True)).spec
    assert sp.dhcp is True and sp.ip == []


def test_exactly_one_addressing_mode_is_required():
    """The provider says "exactly one of dhcp_client, ip, and pppoe". Catching
    it here beats catching it at the device commit, where the message is worse
    and the change is already in a candidate config."""
    with pytest.raises(IntentError, match="exactly one"):
        load_intent(_doc(dhcp=True))            # ip AND dhcp
    with pytest.raises(IntentError, match="addressing mode"):
        load_intent(_doc(ip=[]))                 # neither


@pytest.mark.parametrize("bad,frag", [
    ({"ip": ["10.20.0.1"]}, "CIDR"),
    ({"ip": ["not-an-address/24"]}, "CIDR"),
    ({"ip": "10.0.0.1/24"}, "list of CIDR"),
    ({"mtu": 0}, "positive integer"),
    ({"mtu": "big"}, "positive integer"),
    ({"interface": ""}, "interface"),
])
def test_bad_shapes_are_rejected(bad, frag):
    with pytest.raises(IntentError) as e:
        load_intent(_doc(**bad))
    assert any(frag in str(p) for p in e.value.problems)


def test_the_management_profile_is_catalog_validated():
    cat = NameCatalog(kind="interface management profile",
                      names=frozenset({"auto-vpn-ping-profile"}))
    load_intent(_doc(management_profile="auto-vpn-ping-profile"),
                interface_profile_catalog=cat)
    with pytest.raises(IntentError) as e:
        load_intent(_doc(management_profile="typo"), interface_profile_catalog=cat)
    assert any("interface management profile 'typo'" in str(p) for p in e.value.problems)


# ── compile ───────────────────────────────────────────────────────────────
def test_compiles_to_the_provider_shape():
    """Addressing lives under `layer3`, and EXACTLY ONE of ip / dhcp_client is
    non-null — mirroring scm_ethernet_interface so a reader can diff it against
    `terraform providers schema` without translating."""
    c = compile_any(load_intent(_doc(mtu=1500)), _env())
    got = interface_tfvars([c])["interfaces"]["$eth-local"]
    assert got["folder"] == "prod-edge"
    assert got["layer3"]["ip"] == [{"name": "10.20.0.1/24"}]
    assert got["layer3"]["dhcp_client"] is None
    assert got["layer3"]["mtu"] == 1500


def test_dhcp_compiles_to_dhcp_client_with_ip_null():
    c = compile_any(load_intent(_doc(ip=[], dhcp=True)), _env())
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
    c = compile_any(load_intent(_doc()), _env())
    v = classify_interface(c, current=_cur({}))
    assert v.tier == "HIGH"
    assert "interface_becomes_addressed" in [x["check"] for x in v.checks_fired]


def test_changing_an_already_addressed_interface_is_not_the_same_act():
    from fwgitops.classify import classify_interface
    c = compile_any(load_intent(_doc()), _env())
    v = classify_interface(c, current=_cur({"ip": [{"name": "10.0.0.9/24"}]}))
    assert v.tier == "LOW" and v.checks_fired == ()


def test_dhcp_counts_as_existing_addressing():
    from fwgitops.classify import classify_interface
    c = compile_any(load_intent(_doc()), _env())
    v = classify_interface(c, current=_cur({"dhcp_client": {"enable": True}}))
    assert v.checks_fired == ()


def test_without_a_snapshot_the_check_is_skipped_not_guessed():
    from fwgitops.classify import classify_interface
    assert classify_interface(compile_any(load_intent(_doc()), _env())).checks_fired == ()


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
