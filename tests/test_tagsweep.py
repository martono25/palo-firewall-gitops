"""Tag lifecycle: Terraform creates, a separate sweep removes (ADR-0009).

The 409 this exists to prevent was MEASURED (spike/tag-destroy-ordering,
2026-08-10): Terraform ran a tag DESTROY before the rule UPDATE that released it.
So the two halves are separated in time, and the sweep must be conservative —
deleting a referenced tag deliberately would be worse than the bug.
"""

from __future__ import annotations

import pytest

from fwgitops.tagsweep import (
    GITOPS_PREFIX,
    TagPlan,
    ensure_tags,
    plan_tags,
    sweep_tags,
)


class FakeSession:
    """Records requests; serves canned reads."""

    def __init__(self, tags=(), refs=None, fail_on=None):
        self._tags = list(tags)
        self._refs = refs or {}
        self.calls = []
        self.fail_on = fail_on

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if self.fail_on and self.fail_on in path:
            raise RuntimeError("SCM read failed")
        if method == "GET" and path.endswith("/tags"):
            return {"data": [{"name": n, "id": f"id-{n}"} for n in self._tags]}
        if method == "GET":
            return {"data": self._refs.get(path, [])}
        return {}


SCOPE = {"folder": "prod-edge"}


# ── the two safety rules ──────────────────────────────────────────────────
def test_a_foreign_tag_is_NEVER_swept():
    """A tag this platform did not create is not ours to tidy, whatever
    references it. Only the `gitops:` namespace is in scope."""
    s = FakeSession(tags=["someone-elses-tag", "gitops:req:REQ-1"])
    plan = sweep_tags(s, SCOPE, wanted=[])
    assert plan.unreferenced == ["gitops:req:REQ-1"]
    assert plan.foreign == 1
    deleted = [p for m, p, _ in s.calls if m == "DELETE"]
    assert not any("someone-elses" in p for p in deleted)


def test_a_REFERENCED_tag_is_never_swept():
    """Deleting one is the exact 409 this design avoids — done deliberately,
    which is worse than hitting it by accident."""
    s = FakeSession(
        tags=["gitops:req:REQ-1"],
        refs={"/config/security/v1/security-rules": [{"tag": ["gitops:req:REQ-1"]}]})
    plan = sweep_tags(s, SCOPE, wanted=[])
    assert plan.unreferenced == [] and plan.referenced == ["gitops:req:REQ-1"]
    assert not [p for m, p, _ in s.calls if m == "DELETE"]


def test_references_are_read_from_SCM_not_from_the_intent_tree():
    """An object created OUTSIDE GitOps can reference a `gitops:` tag. Deriving
    references from our own intents would delete it and break their config to
    tidy ours."""
    s = FakeSession(
        tags=["gitops:req:REQ-GONE"],
        refs={"/config/objects/v1/addresses": [{"tag": ["gitops:req:REQ-GONE"]}]})
    assert sweep_tags(s, SCOPE, wanted=[]).unreferenced == []


def test_a_failed_reference_read_sweeps_NOTHING():
    """A partial reference set makes a referenced tag look unreferenced. Fail
    loudly rather than delete on incomplete information."""
    s = FakeSession(tags=["gitops:req:REQ-1"], fail_on="security-rules")
    with pytest.raises(RuntimeError):
        sweep_tags(s, SCOPE, wanted=[])
    assert not [p for m, p, _ in s.calls if m == "DELETE"]


def test_a_tag_the_NEXT_apply_wants_is_not_swept():
    """The window between ensure and apply: a tag can exist, be referenced by
    nothing yet, and still be needed. `wanted` protects it."""
    s = FakeSession(tags=["gitops:req:REQ-NEW"])
    assert sweep_tags(s, SCOPE, wanted=["gitops:req:REQ-NEW"]).unreferenced == []


# ── ensure ────────────────────────────────────────────────────────────────
def test_ensure_creates_only_what_is_missing():
    s = FakeSession(tags=["gitops:managed"])
    plan = ensure_tags(s, SCOPE, wanted=["gitops:managed", "gitops:req:REQ-1"])
    assert plan.missing == ["gitops:req:REQ-1"]
    posts = [b["name"] for m, p, b in s.calls if m == "POST"]
    assert posts == ["gitops:req:REQ-1"], "an existing tag must not be re-created"


def test_ensure_NEVER_deletes():
    """It is the half that runs BEFORE apply, while references still exist."""
    s = FakeSession(tags=["gitops:req:REQ-OLD"])
    ensure_tags(s, SCOPE, wanted=["gitops:req:REQ-NEW"])
    assert not [p for m, p, _ in s.calls if m == "DELETE"]


def test_ensure_ignores_a_non_gitops_name():
    """Nothing outside the namespace is created either — the platform manages
    its own tags and no others."""
    s = FakeSession()
    ensure_tags(s, SCOPE, wanted=["not-ours", "gitops:managed"])
    assert [b["name"] for m, p, b in s.calls if m == "POST"] == ["gitops:managed"]


def test_dry_run_writes_nothing():
    s = FakeSession(tags=["gitops:req:REQ-1"])
    ensure_tags(s, SCOPE, wanted=["gitops:req:REQ-2"], dry_run=True)
    sweep_tags(s, SCOPE, wanted=[], dry_run=True)
    assert not [m for m, _, _ in s.calls if m in ("POST", "DELETE")]


def test_plan_is_pure_and_reports_all_four_buckets():
    plan = plan_tags(
        wanted=["gitops:req:A", "gitops:req:B"],
        present={"gitops:req:A": "1", "gitops:req:C": "2", "theirs": "3"},
        used={"gitops:req:A"})
    assert plan.missing == ["gitops:req:B"]
    assert plan.unreferenced == ["gitops:req:C"]
    assert plan.referenced == ["gitops:req:A"]
    assert plan.foreign == 1
