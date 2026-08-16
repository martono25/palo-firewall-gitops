"""A violation must survive the run that found it.

Drift failed the nightly job and left nothing behind: the classification existed
in code and never reached disk, so a finding could not be aged, counted, routed
into a follow-up process, or produced for an assessor later. CI logs expire —
the assessor guide says so about `plan_sha256` already.
"""

from __future__ import annotations

import json

import pytest

from fwgitops.violations import (
    CLASSES,
    SCHEMA,
    build,
    load,
    reconcile,
    record_path,
    summarise,
    write,
)

RUN = "https://example/run/1"
T1 = "2026-08-16T01:00:00Z"
T2 = "2026-08-17T01:00:00Z"
T3 = "2026-08-18T01:00:00Z"


def _found(cls="unmanaged", name="Testing-unmmanaged", scope="GitOps", tags=()):
    return {"cls": cls, "kind": "security-rule", "scope": scope,
            "name": name, "tags": list(tags)}


def test_the_classes_are_distinct_and_named():
    """They are different failures of authorisation and a report must not merge
    them. `malformed` in particular is worse than an honest stranger: it CLAIMS
    this platform's provenance while tracing to no request, so it would pass any
    check that only looked for the marker."""
    assert set(CLASSES) == {"unmanaged", "malformed", "orphaned", "reordered"}
    # `reordered` is the quietest of them: every rule is authorised and
    # unmodified, so nothing looks wrong on inspection while the EFFECTIVE
    # policy has changed. It ranks second for that reason.
    assert CLASSES.index("reordered") == 1
    with pytest.raises(ValueError):
        build(cls="suspicious", kind="security-rule", scope="GitOps",
              name="x", tags=[], run_url=RUN, at=T1)


def test_a_new_violation_is_recorded_as_open(tmp_path):
    changed = reconcile(found=[_found()], existing={}, root=tmp_path,
                        run_url=RUN, at=T1)
    assert len(changed) == 1
    _, rec = changed[0]
    assert rec["schema"] == SCHEMA
    assert rec["class"] == "unmanaged"
    assert rec["status"] == "open"
    assert rec["first_seen"] == rec["last_seen"] == T1


def test_the_SAME_violation_on_ten_nights_is_ONE_record(tmp_path):
    """Findings, not events. Seven records for one violation is how a real
    finding drowns, and it makes "open for six days" impossible to see."""
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    existing = load(tmp_path)
    write(reconcile(found=[_found()], existing=existing, root=tmp_path,
                    run_url=RUN, at=T2))

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, "one violation, one record"
    rec = json.loads(files[0].read_text())
    assert rec["first_seen"] == T1, "when it appeared is the useful part"
    assert rec["status"] == "open"
    # `last_seen` deliberately does NOT advance on a re-detection that changed
    # nothing: it advances on `now()` every run, so writing it would rewrite
    # every record nightly and open a pull request that says nothing. It means
    # "when this record last CHANGED". Whether a finding is still open is
    # answered by `resolved_at` being null. See the two tests below.
    assert rec["last_seen"] == T1


def test_a_run_where_nothing_changed_writes_NOTHING(tmp_path):
    """Otherwise every nightly run opens a pull request that says nothing, and
    the ones that matter stop being read."""
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    existing = load(tmp_path)
    changed = reconcile(found=[_found()], existing=existing, root=tmp_path,
                        run_url=RUN, at=T1)      # same run, same timestamp
    assert changed == []


def test_a_violation_that_goes_away_is_RESOLVED_not_deleted(tmp_path):
    """"This was open for six days in August" is exactly what a follow-up
    process needs afterwards. Deleting the record destroys it."""
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    existing = load(tmp_path)

    changed = reconcile(found=[], existing=existing, root=tmp_path, run_url=RUN,
                        at=T2, scopes_checked=["GitOps"])
    write(changed)

    rec = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert rec["status"] == "resolved"
    assert rec["resolved_at"] == T2
    assert rec["first_seen"] == T1, "the history survives resolution"


def test_a_scope_that_was_NOT_CHECKED_does_not_resolve_its_violations(tmp_path):
    """The difference between "we looked and it is gone" and "we did not look".

    A folder whose read failed, or that was skipped, must not silently close
    every finding in it — that would turn an outage in the checker into a clean
    bill of health, which is the failure this whole subsystem exists to avoid.
    """
    write(reconcile(found=[_found(scope="prod-edge")], existing={}, root=tmp_path,
                    run_url=RUN, at=T1))
    existing = load(tmp_path)

    # This run only managed to check GitOps.
    changed = reconcile(found=[], existing=existing, root=tmp_path, run_url=RUN,
                        at=T2, scopes_checked=["GitOps"])
    assert changed == [], "prod-edge was not checked, so nothing in it resolves"


def test_a_violation_that_RETURNS_reopens_the_same_record(tmp_path):
    """Someone deletes it, someone re-adds it. That is the same finding coming
    back, and when it FIRST appeared is the part worth keeping."""
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    write(reconcile(found=[], existing=load(tmp_path), root=tmp_path, run_url=RUN,
                    at=T2, scopes_checked=["GitOps"]))
    write(reconcile(found=[_found()], existing=load(tmp_path), root=tmp_path,
                    run_url=RUN, at=T3))

    assert len(list(tmp_path.glob("*.json"))) == 1
    rec = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert rec["status"] == "open"
    assert rec["resolved_at"] is None
    assert rec["first_seen"] == T1, "first_seen must never be overwritten"
    assert rec["last_seen"] == T3


def test_a_record_lands_where_it_belongs_even_with_a_hostile_name(tmp_path):
    """SCM names may contain slashes and spaces. The parent must BE the
    violations directory — "did it escape the root" is the wrong question, since
    `../..` from a subdirectory can land back inside it and still be wrong."""
    p = record_path(tmp_path, scope="GitOps", kind="security-rule",
                    name="../../etc/passwd")
    write([(p, build(cls="unmanaged", kind="security-rule", scope="GitOps",
                     name="../../etc/passwd", tags=[], run_url=RUN, at=T1))])
    assert p.resolve().parent == tmp_path.resolve()
    assert "/" not in p.name and ".." not in p.name
    assert json.loads(p.read_text())["name"] == "../../etc/passwd", (
        "the RECORD keeps the true name; only the filename is sanitised")


def test_a_malformed_record_on_disk_does_not_lose_the_run(tmp_path):
    """Fail soft on READING history, hard on detecting. A corrupt old record is
    not a reason to stop reporting today's violations."""
    (tmp_path / "junk.json").write_text("{not json")
    assert load(tmp_path) == {}


def test_the_summary_leads_with_the_worst_class(tmp_path):
    recs = [
        build(cls="orphaned", kind="security-rule", scope="p", name="o",
              tags=[], run_url=RUN, at=T1),
        build(cls="malformed", kind="security-rule", scope="p", name="m",
              tags=[], run_url=RUN, at=T1),
    ]
    out = summarise(recs)
    assert out.index("malformed") < out.index("orphaned")
    assert "2 open violation(s)" in out
    assert summarise([]) == "no open violations"


def test_an_EMPTY_checked_set_resolves_NOTHING(tmp_path):
    """"We checked nothing" must never read as "we checked everything".

    `fwgitops snapshot` writes a bare list of rows, so a folder whose SCM read
    failed or returned nothing yields NO scopes at all. The guard used to be
    `if scopes_checked and ...`, which skipped itself entirely on an empty set
    and closed every open violation — turning an outage in the checker into a
    clean bill of health. That is the one failure this module exists to prevent,
    and it sat on the default path.
    """
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    changed = reconcile(found=[], existing=load(tmp_path), root=tmp_path,
                        run_url=RUN, at=T2)          # nothing named as checked
    assert changed == [], "no scope was named, so no finding may close"
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["status"] == "open"


def test_a_LATER_run_finding_the_SAME_violation_still_writes_nothing(tmp_path):
    """The stricter version of the test above, and the one that matters.

    That test reuses a single timestamp, so it passes even when `last_seen`
    alone counts as a change. In production `at` is `now()` and always differs —
    so the same three findings would have rewritten their records and opened a
    pull request EVERY NIGHT, each saying nothing new. Pull requests nobody
    needs to read are how the ones that matter stop being read.
    """
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    changed = reconcile(found=[_found()], existing=load(tmp_path), root=tmp_path,
                        run_url="https://example/run/2", at=T2)   # a LATER night
    assert changed == [], "nothing about the violation changed, so nothing writes"


def test_but_a_violation_that_CHANGES_CLASS_does_write(tmp_path):
    """The suppression above must not swallow a real transition — someone
    adding a `gitops:managed` marker to an unmanaged rule turns a stranger into
    a forgery, and that is a different finding to act on."""
    write(reconcile(found=[_found(cls="unmanaged")], existing={}, root=tmp_path,
                    run_url=RUN, at=T1))
    changed = reconcile(found=[_found(cls="malformed", tags=["gitops:managed"])],
                        existing=load(tmp_path), root=tmp_path, run_url=RUN, at=T2)
    assert len(changed) == 1 and changed[0][1]["class"] == "malformed"


def test_the_SAME_NAME_in_two_scopes_gets_two_DIFFERENT_ids():
    """The scope is part of the IDENTITY, so it must be part of the id.

    Ids are readable rather than hashed, which makes it tempting to build them
    from the object name alone — and `test-unmanaged-2` in GitOps then means the
    same thing as `test-unmanaged-2` in prod-edge. Two distinct findings sharing
    one identifier silently merges them in the join the id exists to enable, and
    a deletion record pointing at that id would not say which unauthorised
    change it actually removed.

    Caught by mutation: dropping `scope` from the id left every other test in
    this suite green.
    """
    from fwgitops.violations import violation_id

    a = violation_id(scope="GitOps", name="dup", first_seen="2026-08-16T01:00:00Z")
    b = violation_id(scope="prod-edge", name="dup", first_seen="2026-08-16T01:00:00Z")
    assert a != b, "same name in two folders is two findings, not one"
    assert "GitOps" in a and "prod-edge" in b


def test_an_id_SURVIVES_a_resolve_and_return_cycle(tmp_path):
    """A finding that comes back is the same finding, and must keep the id it
    was first known by — `first_seen` is never overwritten, which is what makes
    the id stable without an allocator."""
    write(reconcile(found=[_found()], existing={}, root=tmp_path, run_url=RUN, at=T1))
    first = json.loads(next(tmp_path.glob("*.json")).read_text())["id"]

    write(reconcile(found=[], existing=load(tmp_path), root=tmp_path, run_url=RUN,
                    at=T2, scopes_checked=["GitOps"]))
    write(reconcile(found=[_found()], existing=load(tmp_path), root=tmp_path,
                    run_url=RUN, at=T3, scopes_checked=["GitOps"]))

    assert json.loads(next(tmp_path.glob("*.json")).read_text())["id"] == first


def test_two_findings_CANNOT_COLLAPSE_INTO_ONE_RECORD(tmp_path):
    """`web/1` and `web 1` sanitise to the same filename AND the same id.

    The filename half predates ids entirely — `record_path` has always
    sanitised — so the second finding silently OVERWROTE the first and vanished.
    Invisible by construction: the result is one file where there should be two,
    and nothing was left to notice. Found while mutation-testing the id.
    """
    import pytest

    from fwgitops.violations import build, record_path

    a = build(cls="unmanaged", kind="security-rule", scope="GitOps",
              name="web/1", tags=[], run_url=RUN, at=T1)
    b = build(cls="unmanaged", kind="security-rule", scope="GitOps",
              name="web 1", tags=[], run_url=RUN, at=T1)
    assert a["id"] == b["id"], "the collision this guard exists for"

    write([(record_path(tmp_path, scope="GitOps", kind="security-rule",
                        name="web/1"), a)])
    with pytest.raises(ValueError, match="already holds the finding"):
        write([(record_path(tmp_path, scope="GitOps", kind="security-rule",
                            name="web 1"), b)])
