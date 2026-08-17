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


def test_a_DISABLED_rule_is_caught():
    """The easiest unauthorised change there is, and it was invisible.

    Toggle a managed rule off in the console: it still exists, still carries its
    tags, still matches its request name. The tag engine, the order check and
    every compared field agree nothing is wrong — while the rule does nothing at
    all. Disable a deny and a path opens; disable an allow and a service stops.

    The compiler has always declared `disabled=False`, so this was a plain
    omission from the field map, not a field nobody could assert.
    """
    d = compare(_declared(), dict(LIVE, disabled=True), scope="prod-edge")
    assert [f.field for f in d.fields] == ["disabled"]


def test_a_rule_DECLARED_disabled_is_not_drift_when_it_IS_disabled():
    """An intent may legitimately declare a rule disabled — staged but not yet
    in force. The check must compare, not assume False."""
    assert compare(_declared(disabled=True), dict(LIVE, disabled=True),
                   scope="prod-edge").is_clean


def test_the_THREAT_PROFILE_is_compared_across_differing_shapes():
    """Strip the profile group in the console and IPS/AV stops applying while
    every other field looks identical — the rule matches the same traffic, it
    just stops being inspected.

    The shapes differ: the compiler carries a group NAME, SCM returns
    `{"group": [name]}`. Confirmed against REQ-2026-0812 on the tenant rather
    than read off the Terraform module, which says what we WRITE, not what the
    API returns.
    """
    live = dict(LIVE, profile_setting={"group": ["best-practice"]})
    assert compare(_declared(profile_group="best-practice"), live,
                   scope="prod-edge").is_clean

    d = compare(_declared(profile_group="best-practice"),
                dict(LIVE, profile_setting=None), scope="prod-edge")
    assert [f.field for f in d.fields] == ["profile_setting"], (
        "a profile removed in the console must be caught")


def test_a_rule_declaring_NO_profile_is_not_compared_on_it():
    """Most rules declare none, and asserting a value this platform never writes
    is the false positive that hit every rule in the estate once already."""
    assert compare(_declared(), dict(LIVE, profile_setting={"group": ["x"]}),
                   scope="prod-edge").is_clean


def test_a_rule_declared_DISABLED_reaches_terraform_as_disabled():
    """The tfvars entry is not decoration.

    The module declares `disabled = optional(bool, false)`, so omitting the key
    does not break the apply — Terraform just writes `false`. Which means a rule
    an intent declares DISABLED would be applied ENABLED, silently, and the
    comparison would then report drift on a rule that Terraform itself had just
    switched on.

    Dropping `"disabled": r.disabled` from the tfvars failed no test until this
    one existed.
    """
    import dataclasses

    from fwgitops.compiler import CompiledChange, to_tfvars_written

    # A REAL CompiledChange, not a stub: `to_tfvars` also reads the object
    # collections, and a stub with only `.rule` fails on the first of them —
    # which is the test discovering the payload has more contract than it
    # assumed.
    off = _declared(disabled=True)
    fields = {f.name for f in dataclasses.fields(CompiledChange)}
    kwargs = {"rule": off}
    for name in fields - {"rule"}:
        f = next(x for x in dataclasses.fields(CompiledChange) if x.name == name)
        if f.default is not dataclasses.MISSING:
            kwargs[name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            kwargs[name] = f.default_factory()             # type: ignore[misc]
        else:
            kwargs[name] = () if "objects" in name else None
    payload = to_tfvars_written([CompiledChange(**kwargs)])
    row = payload["security_rules"][off.name]
    assert row.get("disabled") is True, (
        "a rule declared disabled must reach Terraform as disabled; the module "
        "default would otherwise apply it ENABLED")


def test_an_UNDECLARED_log_profile_falls_back_to_the_environment_DEFAULT():
    """Closing the gap without declaring the default in every intent.

    Every prod-edge rule carried `log_setting: "Cortex Data Lake"` in SCM while
    the compiler emitted None. Asserting a value this platform never writes
    reported drift no remediation could fix; skipping it left a real change
    invisible. Naming the default in the environment resolves both — the
    declared state is complete, and any OTHER value is somebody changing it.
    """
    import yaml
    from pathlib import Path

    from fwgitops.resolve import EnvMap

    root = Path(__file__).resolve().parents[1]
    em = EnvMap.from_dict(yaml.safe_load((root / "catalog" / "environments.yaml").read_text()))
    assert em.resolve("prod").default_log_forwarding == "Cortex Data Lake", (
        "the environment must NAME the default, or an undeclared log profile is "
        "uncomparable again")


def test_a_CHANGED_log_profile_is_now_a_finding():
    """The point of declaring the default: any other value is a change."""
    d = compare(_declared(log_setting="Cortex Data Lake"),
                dict(LIVE, log_setting="somewhere-else"), scope="prod-edge")
    assert [f.field for f in d.fields] == ["log_setting"]


def test_compiling_a_REAL_intent_applies_the_environment_default():
    """The fallback itself, exercised.

    Removing `or res.default_log_forwarding` from the compiler failed no test:
    one test asserted the env map HOLDS the default, another compared a rule
    built by hand with the value already set. Neither ran the line that puts the
    two together — the same gap as `moved=[]` and the fixture that declared
    `log_setting` explicitly.
    """
    import yaml
    from pathlib import Path

    from fwgitops.compiler import compile_request
    from fwgitops.intent import load_intent
    from fwgitops.resolve import EnvMap

    root = Path(__file__).resolve().parents[1]
    em = EnvMap.from_dict(yaml.safe_load((root / "catalog" / "environments.yaml").read_text()))
    cats = {"service_catalog": yaml.safe_load((root / "catalog" / "services.yaml").read_text()),
            "app_catalog": yaml.safe_load((root / "catalog" / "apps.yaml").read_text())}
    doc = yaml.safe_load((root / "intent" / "prod" / "observability"
                          / "REQ-2026-0725.yaml").read_text())
    assert "log_forwarding" not in (doc.get("spec") or {}), (
        "this test needs an intent that declares NO log profile")

    ch = compile_request(load_intent(doc, env_map=em, **cats), em)
    assert ch.rule.log_setting == "Cortex Data Lake", (
        "an intent declaring no log profile must compile to the environment's "
        "default, or the field is uncomparable and a change to it invisible")


def test_an_intent_CAN_declare_a_rule_disabled_and_it_reaches_the_rule():
    """Enforcement removed the alternative, so the pipeline had to provide one.

    Disabling a rule is routine. Once remediation began reverting console
    changes there was no sanctioned way to do it — switch it off by hand and the
    next run switches it back on. The only pipeline path was DELETING the
    intent, which destroys the rule and loses its position in the rulebase: a
    much larger act than "turn this off for now".
    """
    import copy

    import yaml
    from pathlib import Path

    from fwgitops.compiler import compile_request
    from fwgitops.intent import load_intent
    from fwgitops.resolve import EnvMap

    root = Path(__file__).resolve().parents[1]
    em = EnvMap.from_dict(yaml.safe_load((root / "catalog" / "environments.yaml").read_text()))
    cats = {"service_catalog": yaml.safe_load((root / "catalog" / "services.yaml").read_text()),
            "app_catalog": yaml.safe_load((root / "catalog" / "apps.yaml").read_text())}
    doc = yaml.safe_load((root / "intent" / "prod" / "observability"
                          / "REQ-2026-0725.yaml").read_text())

    # UNDECLARED -> ENABLED. A request for access means the rule is in force.
    assert compile_request(load_intent(doc, env_map=em, **cats), em).rule.disabled is False

    off = copy.deepcopy(doc)
    off["spec"]["disabled"] = True
    assert compile_request(load_intent(off, env_map=em, **cats), em).rule.disabled is True, (
        "an intent must be able to say a rule is off, or the only way to do it "
        "is a console change the platform reverts")
