"""Editing or deleting an AUTHORISED rule was the uncovered surface.

The live row below is `REQ-2026-0725` exactly as SCM returned it on 2026-08-16
(probe run 31941528922). Using the real shape matters more here than anywhere
else: the whole risk of this module is false positives from normalisation, and a
fixture invented from the schema would hide precisely those.
"""

from __future__ import annotations

from fwgitops.compiler import SecurityRule
from fwgitops.rulediff import compare, compare_all

LIVE = {
    "action": "allow",
    "application": ["any"],
    "category": ["any"],
    "destination": ["addr-10.20.20.10_32-beed1e7b"],
    "disabled": False,
    "folder": "prod-edge",
    "from": ["local"],
    "id": "8354c45f-fa31-415e-8000-8c0dc441c59b",
    "log_end": True,
    "log_setting": "Cortex Data Lake",
    "log_start": False,
    "name": "REQ-2026-0725",
    "negate_destination": False,
    "negate_source": False,
    "policy_type": "Security",
    "service": ["svc-tcp_443-fd64e1b8"],
    "source": ["addr-10.20.1.0_24-85c1076c"],
    "source_user": ["any"],
    "tag": ["gitops:managed", "gitops:req:REQ-2026-0725",
            "gitops:section:specific-allow", "gitops:ticket:JIRA-20725"],
    "to": ["internet"],
}


def _declared(**over):
    base = dict(
        name="REQ-2026-0725", folder="prod-edge",
        from_zones=["local"], to_zones=["internet"],
        sources=["addr-10.20.1.0_24-85c1076c"],
        destinations=["addr-10.20.20.10_32-beed1e7b"],
        services=["svc-tcp_443-fd64e1b8"],
        action="allow", log_end=True,
        tags=["gitops:managed", "gitops:req:REQ-2026-0725",
              "gitops:section:specific-allow", "gitops:ticket:JIRA-20725"],
        application=["any"], log_setting="Cortex Data Lake",
    )
    base.update(over)
    return SecurityRule(**base)


def test_the_REAL_rule_matches_its_declaration():
    """The check has to be able to pass against the live tenant, or it is noise
    that gets switched off in a week. This is the fixture that proves the
    normalisation was measured rather than guessed."""
    d = compare(_declared(), LIVE, scope="prod-edge")
    assert d.is_clean, d.summary()


def test_SCM_ONLY_FIELDS_are_not_drift():
    """`policy_type` is added by the API and `id`/`folder` are placement
    metadata. We never send them, so a value there is not a change to anything
    this platform declared."""
    live = dict(LIVE, policy_type="Something", id="other", folder="elsewhere")
    assert compare(_declared(), live, scope="prod-edge").is_clean


def test_a_REORDERED_list_is_not_drift():
    """SCM does not promise to preserve the order we sent, and for an address
    list the order carries no meaning. Treating it as significant would report
    drift every time the API returned a list the other way round."""
    live = dict(LIVE, tag=list(reversed(LIVE["tag"])))
    assert compare(_declared(), live, scope="prod-edge").is_clean


def test_an_EMPTY_description_matches_an_undeclared_one():
    """SCM returns "" for an unset description; the compiler carries None.
    Reporting that pair would flag every rule without a description, which is
    most of them — the classic false positive that gets a detector ignored."""
    assert compare(_declared(), dict(LIVE, description=""),
                   scope="prod-edge").is_clean


# ── what it must catch ──────────────────────────────────────────────────────

def test_a_WIDENED_DESTINATION_is_caught():
    """The change that matters most: someone adds a destination in the console
    and the rule now passes traffic no request authorised."""
    live = dict(LIVE, destination=LIVE["destination"] + ["addr-0.0.0.0_0-deadbeef"])
    d = compare(_declared(), live, scope="prod-edge")
    assert not d.is_clean and [f.field for f in d.fields] == ["destination"]


def test_an_EDITED_APPLICATION_is_caught_although_terraform_CANNOT():
    """The provider treats `application` as computed so it never fights enrich
    over App-ID, which means an application edited in the console produces NO
    plan diff. Reading the field directly is the only thing that sees it."""
    d = compare(_declared(), dict(LIVE, application=["ssh"]), scope="prod-edge")
    assert [f.field for f in d.fields] == ["application"]


def test_an_ACTION_FLIPPED_to_deny_is_caught():
    d = compare(_declared(), dict(LIVE, action="deny"), scope="prod-edge")
    assert [f.field for f in d.fields] == ["action"]


def test_a_STRIPPED_TAG_is_caught():
    live = dict(LIVE, tag=["gitops:managed"])
    d = compare(_declared(), live, scope="prod-edge")
    assert [f.field for f in d.fields] == ["tag"]


def test_a_DELETED_rule_is_reported_MISSING():
    """The tag engine cannot see this at all — it classifies rules that ARE in
    SCM, so a rule someone deleted in the console is invisible to it."""
    d = compare(_declared(), None, scope="prod-edge")
    assert d.missing and not d.is_clean
    assert "declared in Git, absent from SCM" in d.summary()


def test_compare_all_returns_ONLY_the_rules_that_differ():
    """A report naming every clean rule is a report nobody reads."""
    other = _declared(name="REQ-2026-0726")
    live_other = dict(LIVE, name="REQ-2026-0726", action="deny")
    out = compare_all([_declared(), other], [LIVE, live_other], scope="prod-edge")
    assert [d.name for d in out] == ["REQ-2026-0726"]


def test_every_compared_field_EXISTS_on_the_compiled_rule():
    """A typo in the field map compares None against a real value and reports
    permanent drift on every rule — confidently, and on the field it cannot
    read."""
    from fwgitops.rulediff import FIELD_MAP

    r = _declared()
    for mine in FIELD_MAP:
        assert hasattr(r, mine), f"{mine!r} is not a field of SecurityRule"


def test_a_PROVENANCE_ONLY_snapshot_is_not_comparable(capsys):
    """`{folder, name, tags}` is enough for the tag engine and holds nothing to
    compare. Without this, every declared rule reads as modified against an
    empty field — a confident, total false positive, and how a detector gets
    switched off in its first week."""
    thin = {"folder": "prod-edge", "name": "REQ-2026-0725",
            "tag": ["gitops:managed"]}
    assert compare(_declared(), thin, scope="prod-edge").is_clean


def test_a_row_WITH_fields_is_still_compared():
    """The guard must not swallow a real snapshot: one compared key is enough."""
    from fwgitops.rulediff import carries_content

    assert carries_content(LIVE)
    assert not carries_content({"folder": "f", "name": "n"})


def test_an_UNDECLARED_field_is_not_compared():
    """The false positive that hit every rule in the estate on the first live run.

    No intent declares log forwarding, so the compiler emits None — while SCM
    holds 'Cortex Data Lake', a value nothing here wrote and Terraform cannot
    clear (optional-computed: a null config means "leave alone"). Comparing it
    reported six rules modified and filed six violation records, none of which
    any remediation could resolve.
    """
    undeclared = _declared(log_setting=None)
    assert compare(undeclared, LIVE, scope="prod-edge").is_clean


def test_a_DECLARED_field_is_still_compared_when_it_differs():
    """The guard must not become "compare nothing": declaring a value is what
    makes it enforceable."""
    d = compare(_declared(log_setting="log-best"), LIVE, scope="prod-edge")
    assert [f.field for f in d.fields] == ["log_setting"]


def test_an_undeclared_field_being_SET_is_the_accepted_blind_spot():
    """Pinned so the trade is deliberate rather than forgotten.

    A field the intent leaves unset can be changed in the console without this
    noticing. Declaring it makes it enforceable — the remedy is in the intent,
    not in this comparison.
    """
    d = compare(_declared(description=None), dict(LIVE, description="edited by hand"),
                scope="prod-edge")
    assert d.is_clean
