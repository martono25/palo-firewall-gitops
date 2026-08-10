"""Issue Form -> intent YAML (the broad-requester intake).

The parsing is where the bugs live: an Issue Form arrives as RENDERED MARKDOWN,
not structured data. And the audience cannot debug it — a requester who gets a
broken PR has no way to fix it, so every rejection has to name the form field
they filled in.
"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from fwgitops.intake import FIELD_LABELS, IntakeError, build_intent, parse_form

BODY = """### Change ticket

JIRA-12345

### Why do you need this?

Web tier needs to reach the payments API

### Environment

prod

### Allow or deny?

allow

### Team or app name

payments

### Source

10.20.1.0/24

### Destination

10.20.9.10/32

### Service

tcp/443
"""


def _build(body=BODY, n=42, author="jane@corp"):
    return build_intent(body, issue_number=n, author=author, today=date(2026, 8, 10))


# ── the contract with the form file ───────────────────────────────────────
def test_every_parser_label_exists_in_the_ACTUAL_form():
    """The parser keys on label TEXT, so renaming a label in the form would
    silently produce an empty field and a rejected request the requester cannot
    explain. This is the only thing holding the two files together."""
    from pathlib import Path
    form = yaml.safe_load(
        (Path(__file__).resolve().parents[1]
         / ".github" / "ISSUE_TEMPLATE" / "rule-request.yml").read_text())
    labels = {b["attributes"]["label"] for b in form["body"] if "attributes" in b
              and "label" in b["attributes"]}
    missing = set(FIELD_LABELS) - labels
    assert not missing, f"parser expects labels the form does not have: {missing}"


def test_a_required_form_field_is_required_by_the_parser_too():
    """Both files must agree on what is mandatory, or the form lets someone
    submit something the parser then rejects."""
    from pathlib import Path
    form = yaml.safe_load(
        (Path(__file__).resolve().parents[1]
         / ".github" / "ISSUE_TEMPLATE" / "rule-request.yml").read_text())
    for b in form["body"]:
        if b.get("validations", {}).get("required") and b.get("id") != "confirm":
            label = b["attributes"]["label"]
            assert label in FIELD_LABELS, f"{label!r} is required but the parser ignores it"


# ── the happy path ────────────────────────────────────────────────────────
def test_a_filled_form_becomes_a_VALID_intent():
    """End to end: the generated document must pass the real loader, not merely
    look right. An intake that emits something the compiler rejects is worse than
    no intake — it puts the failure on the requester."""
    from fwgitops.intent import load_intent
    from fwgitops.resolve import EnvMap
    out = _build()
    env = EnvMap.from_dict(
        {"prod": {"folder": "prod-edge", "from_zone": "trust", "to_zone": "app"}})
    ar = load_intent(out.doc, env_map=env)
    assert ar.metadata.id == "REQ-2026-42"
    assert ar.spec.action == "allow"


def test_the_id_comes_from_the_ISSUE_NUMBER():
    """Unique by construction, and it traces the rule on the firewall back to the
    conversation that asked for it. Nobody allocates an id by hand."""
    assert _build(n=7).req_id == "REQ-2026-7"


def test_the_requester_is_the_ISSUE_AUTHOR_not_a_typed_field():
    """A requester field someone types is a field someone can type wrongly, and
    the audit chain hangs off knowing who actually asked."""
    assert _build(author="bob@corp").doc["metadata"]["requester"] == "bob@corp"
    assert "requester" not in FIELD_LABELS.values()


def test_the_team_only_decides_the_PATH():
    out = _build()
    assert out.path == "intent/prod/payments/REQ-2026-42.yaml"
    assert "team" not in out.doc["spec"] and "team" not in out.doc["metadata"]


# ── rejections a requester can act on ─────────────────────────────────────
def test_a_bare_IP_is_rejected_rather_than_guessed():
    """Guessing /24 would silently WIDEN the rule to 254 hosts the requester did
    not ask for. The message says exactly what to write."""
    body = BODY.replace("10.20.9.10/32", "10.20.9.10")
    with pytest.raises(IntakeError) as ei:
        _build(body)
    msg = " ".join(ei.value.problems)
    assert "Destination" in msg and "10.20.9.10/32" in msg


def test_icmp_needs_no_port_and_a_port_is_refused():
    out = _build(BODY.replace("tcp/443", "icmp"))
    assert out.doc["spec"]["service"] == [{"protocol": "icmp"}]
    with pytest.raises(IntakeError) as ei:
        _build(BODY.replace("tcp/443", "icmp/8"))
    assert "icmp" in " ".join(ei.value.problems).lower()


def test_an_unknown_protocol_names_the_FORM_FIELD():
    """A requester cannot act on `spec.service[0].protocol`."""
    with pytest.raises(IntakeError) as ei:
        _build(BODY.replace("tcp/443", "sctp/132"))
    assert "Service:" in " ".join(ei.value.problems)


def test_a_blank_required_field_names_the_LABEL_the_requester_saw():
    with pytest.raises(IntakeError) as ei:
        _build(BODY.replace("JIRA-12345", "_No response_"))
    assert "Change ticket" in " ".join(ei.value.problems)


def test_multiple_endpoints_one_per_line():
    out = _build(BODY.replace("10.20.1.0/24", "10.20.1.0/24\n10.20.2.0/24"))
    assert out.doc["spec"]["source"] == [{"cidr": "10.20.1.0/24"}, {"cidr": "10.20.2.0/24"}]


def test_app_and_fqdn_forms_are_understood():
    out = _build(BODY.replace("10.20.1.0/24", "app: payments-api")
                     .replace("10.20.9.10/32", "fqdn: payments.internal"))
    assert out.doc["spec"]["source"] == [{"app": "payments-api"}]
    assert out.doc["spec"]["destination"] == [{"fqdn": "payments.internal"}]


def test_a_catalog_service_name_is_passed_through():
    out = _build(BODY.replace("tcp/443", "https"))
    assert out.doc["spec"]["service"] == [{"name": "https"}]


def test_ALL_problems_are_reported_at_once():
    """A requester should fix one round of errors, not discover them one at a
    time across three failed PRs."""
    body = BODY.replace("10.20.9.10/32", "nonsense").replace("tcp/443", "sctp/1")
    with pytest.raises(IntakeError) as ei:
        _build(body)
    assert len(ei.value.problems) >= 2


# ── the workflow around it ────────────────────────────────────────────────
def _intake_workflow():
    from pathlib import Path
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "intake.yml").read_text())


def test_the_issue_body_is_never_spliced_into_the_shell():
    """An issue body is attacker-controlled text from anyone who can open an
    issue. `${{ github.event.issue.body }}` inside `run:` substitutes BEFORE bash
    parses the line, so a body containing $(...) would execute in a job holding
    contents: write."""
    wf = _intake_workflow()
    for s in wf["jobs"]["intake"]["steps"]:
        assert "github.event.issue.body" not in str(s.get("run", "")), (
            "the issue body must arrive via env:, never interpolated into the script")


def test_intake_opens_a_PR_and_applies_NOTHING():
    """An Issue Form is a way to WRITE a request, not a way to skip reviewing
    one. It must not touch a firewall."""
    body = " ".join(str(s.get("run", "")) for s in _intake_workflow()["jobs"]["intake"]["steps"])
    for forbidden in ("terraform", "fwgitops push", "fwgitops enrich", "fwgitops tags"):
        assert forbidden not in body, f"intake must not run {forbidden!r}"
    assert "gh pr create" in body


def test_a_rejected_form_tells_the_REQUESTER():
    """A failed run they never see is a request that silently goes nowhere."""
    steps = _intake_workflow()["jobs"]["intake"]["steps"]
    fix = [s for s in steps if "Tell the requester" in (s.get("name") or "")]
    assert fix, "a rejection must be reported back on the issue"
    assert "gh issue comment" in fix[0]["run"]
    assert "intake-err" in fix[0]["run"], "the comment must carry the actual problems"


def test_the_generated_intent_is_validated_BEFORE_the_PR_is_opened():
    """A generated file that fails `compile` would put the platform's own bug in
    front of the requester as if it were their mistake."""
    steps = _intake_workflow()["jobs"]["intake"]["steps"]
    names = [s.get("name") or "" for s in steps]
    iv = next(i for i, n in enumerate(names) if "Validate" in n)
    ip = next(i for i, n in enumerate(names) if "Open the pull request" in n)
    assert iv < ip
    assert "fwgitops compile intent --check" in steps[iv]["run"]


def test_the_pr_step_never_discards_the_error_it_falls_back_from():
    """MEASURED 2026-08-10, on the first live run. `gh pr create` failed because
    the repository had not enabled "Allow GitHub Actions to create and approve
    pull requests". The step sent its stderr to /dev/null and fell back to
    `gh pr edit`, which reported only "no pull requests found for branch" — so
    the run showed a symptom with no trace of the cause.

    A fallback that hides the error it is falling back FROM converts one clear
    failure into a misleading one, which is strictly worse than not having the
    fallback at all. The requester must also learn what happened: the branch is
    pushed and the intent generated, so the request is recoverable, and a run
    that fails silently makes it look lost."""
    from pathlib import Path
    wf = yaml.safe_load(
        (Path(__file__).resolve().parents[1]
         / ".github" / "workflows" / "intake.yml").read_text())
    step = [s for s in wf["jobs"]["intake"]["steps"]
            if s.get("name") == "Open the pull request"][0]["run"]
    # The property is about what RUNS. The comment above the fix says
    # `2>/dev/null` in order to name what went wrong, and a test that could not
    # tell prose from code would forbid explaining the bug it guards.
    code = "\n".join(l for l in step.splitlines() if not l.lstrip().startswith("#"))
    assert "gh pr create" in code
    assert "2>/dev/null" not in code, "the failure reason must survive"
    assert "cat /tmp/pr-err.txt" in code, "and must reach the run log"
    assert "gh issue comment" in code and "compare/main" in code, (
        "a requester whose PR could not be opened must be told, and told how "
        "to finish it — the branch and the intent both exist")
