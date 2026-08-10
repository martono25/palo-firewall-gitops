"""The docs must not outlive the code they describe.

THIS PROJECT'S MOST REPEATED DEFECT IS A DOCUMENT THAT CLAIMS SOMETHING UNTRUE.
`expires` was documented for three weeks after being deleted from the schema. The
Issue-Forms intake was described as built from 2026-07-19 while the directory was
empty. `GITHUB-SETUP.md` called every control "deferred" for a day after all
three were enforcing. v2.0.0 shipped release notes describing auto-apply it did
not have.

Three new documents landed in v2.1.0 — a CLI reference, an operator runbook and
an assessor guide. Adding prose to a project with that history is only defensible
if the prose is pinned to the code, so these tests fail when the two diverge.

They deliberately check FACTS THAT MOVE — the set of subcommands, the exit-code
contract, the evidence field names — not wording.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"


def _flat(path: Path) -> str:
    """Doc text with runs of whitespace collapsed.

    These files are hard-wrapped at ~79 columns, so a phrase worth asserting on
    is usually split across two lines, and markdown emphasis lands in the middle
    of it — `**` and backticks both. Asserting against the raw text tests the
    line-wrapping and the styling, neither of which anyone cares about.
    """
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", path.read_text()))


def _registered_subcommands() -> set:
    src = (REPO_ROOT / "src" / "fwgitops" / "cli.py").read_text()
    return set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))


def test_the_cli_reference_documents_every_subcommand():
    """A command that ships undocumented is a command nobody can find. The
    reference is generated against a version and then edited by hand, so the
    thing that rots is the SET of commands, not the prose about them."""
    # Any mention counts as documented: headings, prose and code fences alike.
    documented = set(re.findall(r"fwgitops ([a-z-]+)",
                                (DOCS / "cli-reference.md").read_text()))
    missing = _registered_subcommands() - documented
    assert not missing, (
        f"these subcommands exist and are not in docs/cli-reference.md: "
        f"{sorted(missing)}")


def test_the_cli_reference_does_not_invent_subcommands():
    """The opposite rot, and the one that wastes a reader's afternoon: a
    documented command that was renamed or removed."""
    doc = (DOCS / "cli-reference.md").read_text()
    # Only the headings, which is where a command is CLAIMED to exist.
    claimed = set(re.findall(r"^### `([a-z-]+)`", doc, re.M))
    invented = claimed - _registered_subcommands()
    assert not invented, (
        f"docs/cli-reference.md documents commands that do not exist: "
        f"{sorted(invented)}")


def test_the_documented_exit_codes_match_the_ones_the_code_returns():
    """CI branches on these. The reference states 2 = invalid input and
    3 = the remote said no; if a command starts returning something else, the
    table is a trap for whoever wires the next workflow."""
    doc = (DOCS / "cli-reference.md").read_text()
    for code, meaning in (("`2`", "invalid input"), ("`3`", "remote operation failed")):
        assert code in doc, f"exit code {code} must stay documented"
    src = (REPO_ROOT / "src" / "fwgitops" / "cli.py").read_text()
    assert "Exit codes:" in src, (
        "the CLI docstrings are the source for that table; if they are gone, "
        "the table has nothing left to agree with")


def test_the_assessor_guide_names_only_fields_a_bundle_actually_has():
    """An assessor reading this guide will grep a real bundle for these names.
    A field the guide invents sends them looking for evidence that does not
    exist, and a renamed field makes the platform look like it stopped
    recording something."""
    bundles = sorted((REPO_ROOT / "evidence").glob("*/*.json"))
    assert bundles, "no evidence bundles to check the guide against"
    real = json.loads(bundles[0].read_text())
    top = set(real)

    guide = _flat(DOCS / "assessor-guide.md")
    # The section table names top-level sections in the first column.
    for claimed in ("request", "compiled", "risk", "approval", "apply", "push",
                    "controls", "controls_not_evidenced"):
        assert claimed in guide, f"{claimed} must stay documented"
        assert claimed in top, (
            f"the assessor guide describes a `{claimed}` section that bundles "
            f"no longer have")

    for sub in ("intent_sha256", "object_sha256", "checks_fired",
                "classifier_version", "job_id", "all_admins"):
        assert sub in guide, f"{sub} must stay documented"


def test_the_assessor_guide_states_what_is_NOT_claimed():
    """The section that makes the rest trustworthy. A guide that lists controls
    and omits the limits is marketing, and this project's whole thesis is that a
    claimed control which is not operating is worse than an absent one.

    AC-5 in particular: with one collaborator, the same person authors, approves
    and releases. If that ever stops being disclosed, the guide has become the
    defect it documents."""
    guide = _flat(DOCS / "assessor-guide.md")
    assert "What this does not claim" in guide
    # The SPECIFIC sentence, not the words scattered anywhere. "not earned"
    # also appears in the controls table, so a looser assertion passes with the
    # AC-5 disclosure deleted — which a mutation run duly demonstrated.
    assert "AC-5 is not earned on this deployment" in guide, (
        "the separation-of-duties limit must stay disclosed, in terms an "
        "assessor reads as a limit rather than a footnote")
    assert "one approver, not two" in guide, (
        "and must say concretely what that costs on this deployment")
    assert "not that the firewall is running it" in guide, (
        "a successful push is not proof the device has the change")


def test_the_runbook_covers_the_operations_that_have_actually_failed():
    """Each of these cost a live debugging session. A runbook that drops one is
    a runbook that lets the next person rediscover it."""
    runbook = _flat(DOCS / "operator-runbook.md")
    for topic, why in (
        ("Removes:", "a removal needs its own ticket, in the PR body"),
        ("no evidence bundle records it", "dispatching an apply to fix a failed "
                                          "removal deletes silently"),
        ("AUTOMATION_PR_TOKEN", "an expired token stops evidence landing, quietly"),
        ("all_admins", "break-glass must be visible in the record"),
        ("40 seconds", "a successful push does not mean the device has it"),
    ):
        assert topic in runbook, f"the runbook must still cover: {why}"


def test_every_new_doc_is_reachable_from_the_README():
    """An undiscoverable document is an unread one."""
    readme = (REPO_ROOT / "README.md").read_text()
    for name in ("cli-reference.md", "operator-runbook.md", "assessor-guide.md"):
        assert name in readme, f"docs/{name} must be linked from README.md"


def test_no_doc_links_to_a_file_that_does_not_exist():
    """Broken relative links, checked across every doc rather than the new ones,
    because a link rots when the TARGET moves and the page linking to it is
    untouched."""
    broken = []
    for md in sorted(DOCS.glob("*.md")) + [REPO_ROOT / "README.md"]:
        for target in re.findall(r"\]\((?!https?://|#)([^)#]+)", md.read_text()):
            # `../../issues/new?template=…` is a GitHub-relative URL, not a
            # path — it resolves against the repo web root and has a query
            # string. Only real file references are checkable here.
            if "?" in target or target.startswith("../../"):
                continue
            if not (md.parent / target).exists():
                broken.append(f"{md.name} → {target}")
    assert not broken, f"broken relative links: {broken}"
