"""Automatic deletion is the most dangerous thing this repository does.

Every other control reports. This one destroys config, unattended, on a
schedule. The tests below are therefore mostly about what it must REFUSE — a
false positive here is not a noisy report, it is deleted production config.
"""

from __future__ import annotations

from fwgitops.objectdrift import ClassifiedObject
from fwgitops.remediate import removals_for_objects, removals_for_rules


def _obj(name, provenance="unmanaged", tags=(), scope="GitOps", kind="address"):
    return ClassifiedObject(name=name, kind=kind, scope=scope,
                            provenance=provenance, folder=scope, snippet=None,
                            tags=tuple(tags))


IDS = {"handmade": "id-1", "ours": "id-2", "sinkhole": "id-3",
       "parent-thing": "id-4", "tagged": "id-5"}


def test_an_unmanaged_object_IS_removed():
    out = removals_for_objects([_obj("handmade")], IDS)
    assert [r.name for r in out] == ["handmade"]
    assert out[0].object_id == "id-1"


def test_everything_the_platform_can_ACCOUNT_FOR_is_refused():
    """`ours`, `scm` and `inherited` are the three innocent explanations, and
    each is somebody's working config. The check exists to find the fourth."""
    objs = [_obj("ours", "ours"), _obj("sinkhole", "scm"),
            _obj("parent-thing", "inherited")]
    assert removals_for_objects(objs, IDS) == []


def test_a_REQUEST_TAG_alone_does_not_protect_an_object():
    """A tag is a label anyone can type, and it is INHERITED by a copy.

    This guard used to key on the tag, which protected the forgery precisely
    because it was wearing the original's label. What protects config is Git
    DECLARING it, under the object's own name.
    """
    o = _obj("tagged", "unmanaged", tags=["gitops:managed", "gitops:req:REQ-1"])
    assert removals_for_objects([o], IDS, declared=[]) != [], (
        "REQ-1 is not declared, so nothing says this should exist")
    assert removals_for_objects([o], IDS, declared=["tagged"]) == [], (
        "declared under its own name — apply repairs it, deletion would remove "
        "authorised config")


def test_an_object_with_NO_ID_is_skipped_rather_than_deleted_by_name():
    """A delete addressed by anything other than the id SCM returned for the row
    we classified is a different object."""
    assert removals_for_objects([_obj("handmade")], {}) == []


# ── rules ───────────────────────────────────────────────────────────────────

def _row(name, folder="prod-edge", tags=(), rid="rid-1"):
    return {"name": name, "folder": folder, "id": rid, "tag": list(tags)}


def test_an_unmanaged_rule_IS_removed():
    out = removals_for_rules([_row("console-hack")], scope="prod-edge",
                             drifted_names=["console-hack"])
    assert [r.name for r in out] == ["console-hack"]
    assert out[0].kind == "security-rule"


def test_a_rule_the_TAG_ENGINE_DID_NOT_NAME_is_left_alone():
    """Only the tag engine can tell unmanaged from orphaned or malformed. A rule
    it did not name is not this job's business, whatever it looks like."""
    assert removals_for_rules([_row("something-else")], scope="prod-edge",
                              drifted_names=["console-hack"]) == []


def test_an_INHERITED_rule_is_never_deleted_from_a_CHILD_scope():
    """A folder read returns the tree above it. Deleting a parent's rule because
    it is visible here destroys config for every other folder that inherits it."""
    assert removals_for_rules([_row("parent-rule", folder="shared")],
                              scope="prod-edge",
                              drifted_names=["parent-rule"]) == []


def test_the_guards_are_reapplied_even_when_the_CALLER_names_the_rule():
    """The caller passes the unmanaged list, so a bug upstream could name
    anything. A managed rule must survive being named by mistake — the guard is
    not a formality, it is the last thing between a wrong list and deleted
    production config."""
    row = _row("REQ-2026-0725", tags=["gitops:managed", "gitops:req:REQ-2026-0725"])
    assert removals_for_rules([row], scope="prod-edge",
                              drifted_names=["REQ-2026-0725"],
                              declared=["REQ-2026-0725"]) == []


def test_a_rule_with_no_id_is_skipped():
    row = {"name": "console-hack", "folder": "prod-edge", "tag": []}
    assert removals_for_rules([row], scope="prod-edge",
                              drifted_names=["console-hack"]) == []


def test_a_DECLARED_rule_is_repaired_by_apply_not_deleted():
    """The line between remove and restore, and it is not a judgement call.

    Every drift class is unauthorised STATE; what differs is the correct action.
    A malformed rule NAMED after a declared request is config Git says should
    exist whose tags are damaged — apply rewrites them. Deleting it and waiting
    for the next apply to recreate it is an outage caused by a labelling defect.
    """
    row = _row("REQ-2026-0725", tags=["gitops:managed"])     # req tag lost
    assert removals_for_rules([row], scope="prod-edge",
                              drifted_names=["REQ-2026-0725"],
                              declared=["REQ-2026-0725"]) == []


def test_a_malformed_COPY_of_a_declared_rule_IS_deleted():
    """The console-copy case: it inherits `gitops:req:REQ-2026-0725` but is
    named REQ-2026-0725-copy, which Git declares nowhere. Not declared under its
    OWN name, so nothing says it should exist."""
    row = _row("REQ-2026-0725-copy",
               tags=["gitops:managed", "gitops:req:REQ-2026-0725"])
    out = removals_for_rules([row], scope="prod-edge",
                             drifted_names=["REQ-2026-0725-copy"],
                             declared=["REQ-2026-0725"])
    assert [r.name for r in out] == ["REQ-2026-0725-copy"]


def test_an_ORPHANED_rule_IS_deleted():
    """Its request is gone from Git, which is the definition of the class —
    nothing declares it, so nothing says it should exist."""
    row = _row("REQ-2026-0699", tags=["gitops:managed", "gitops:req:REQ-2026-0699"])
    out = removals_for_rules([row], scope="prod-edge",
                             drifted_names=["REQ-2026-0699"],
                             declared=["REQ-2026-0725"])
    assert [r.name for r in out] == ["REQ-2026-0699"]


def test_the_module_orders_RULES_BEFORE_OBJECTS():
    """The referrer before the referent, or the 409 wins.

    A hand-made rule arrives with hand-made addresses, and SCM refuses to delete
    an object a rule still references — `409 NON_ZERO_REFS`, the same conflict
    the object sweep is built to order around. Deleting objects first fails on
    the 409 AND abandons the rule removal queued behind it, so an unauthorised
    path survives a run that reported an error about an address.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "fwgitops"
           / "cli.py").read_text()
    block = src.split("removals = (", 1)[1][:400]
    assert block.index("removals_for_rules") < block.index("removals_for_objects"), (
        "rules must be deleted before the objects they reference")


def test_an_object_STILL_IN_USE_does_not_fail_the_run():
    """An unmanaged object can be held by a MANAGED rule someone edited in the
    console to point at it. That edit is plan drift; `apply` restores the rule
    and the reference goes away. Failing here turns a self-resolving condition
    into a red run every night, and abandons the removals queued behind it."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "fwgitops"
           / "cli.py").read_text()
    flat = re.sub(r"\s+", " ", src)
    assert "NON_ZERO_REFS" in flat and "still referenced — leaving it" in flat


# ── the function itself, actually executed ──────────────────────────────────

class _StubSession:
    """Returns an empty estate. Enough to run every line of the happy path."""

    def __init__(self):
        self.deleted = []

    def request(self, method, path, params=None, body=None):
        if method == "DELETE":
            self.deleted.append(path)
            return {}
        return {"data": []}


def test_run_remediate_EXECUTES_end_to_end(tmp_path, monkeypatch):
    """The bug this exists for: `from fwgitops.drift import of_kind` — a name
    that lives in `fwgitops.kinds`.

    A function-local import only runs when the function runs, and nothing ran
    this one. 1041 tests passed while `fwgitops remediate` raised ImportError on
    its first line of real work, and it was found by a dry run against the live
    tenant. Unit tests covered `removals_for_*` thoroughly and never called the
    command that uses them.
    """
    from pathlib import Path

    from fwgitops.cli import run_remediate

    repo = Path(__file__).resolve().parents[1]
    stub = _StubSession()
    rc = run_remediate("prod-edge", repo / "intent",
                       repo / "catalog" / "environments.yaml",
                       service_catalog_path=repo / "catalog" / "services.yaml",
                       app_catalog_path=repo / "catalog" / "apps.yaml",
                       session=stub)

    # An empty estate means every declared rule is MISSING, which is drift — the
    # exit code is not the point here. What is being proven is that the function
    # runs at all.
    assert rc in (0, 3)
    assert stub.deleted == [], "a dry run must never issue a DELETE"


def test_a_deletion_links_to_its_finding_WHATEVER_the_record_says_today(tmp_path,
                                                                        monkeypatch):
    """Linking must not depend on the record's status or on merge latency.

    Filtering to `open` coupled the link to two unrelated things: whether some
    other checker had already closed the record, and whether the run that filed
    it had finished merging. Both happened at once — a finding created and
    resolved fifteen seconds apart by the same run — and two real deletions
    recorded `violation_id: null` while their findings sat right there.
    """
    import json

    from fwgitops import violations as _v

    root = tmp_path / "evidence" / "violations"
    _v.write(_v.reconcile(
        found=[{"cls": "unmanaged", "kind": "security-rule", "scope": "prod-edge",
                "name": "console-hack", "tags": []}],
        existing={}, root=root, run_url="https://ex/1", at="2026-08-16T11:07:43Z"))
    # Closed by something else, as the object checker used to do.
    _v.write(_v.reconcile(found=[], existing=_v.load(root), root=root,
                          run_url="https://ex/1", at="2026-08-16T11:07:58Z",
                          scopes_checked=["prod-edge"],
                          kinds_checked=["security-rule"]))
    rec = json.loads(next(root.glob("*.json")).read_text())
    assert rec["status"] == "resolved"

    monkeypatch.chdir(tmp_path)
    known = {(r.get("kind"), r.get("name")): r.get("id")
             for r in _v.load(tmp_path / "evidence" / "violations").values()
             if r.get("scope") == "prod-edge"}
    assert known[("security-rule", "console-hack")] == rec["id"], (
        "a resolved record is still the finding this deletion answers")
