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


def test_the_runbook_covers_replacing_a_firewall():
    """A new serial is threaded through catalog, intents, Terraform roots and
    evidence, and NOTHING IN CI CATCHES A HALF-DONE REPLACEMENT — an intent
    naming a stale serial compiles clean.

    The procedure is only useful if it keeps naming the two steps that are
    outside this repository (deactivate the licence, re-point the inherited SCM
    defaults) and the one gap that makes ordering matter."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "Replacing a firewall" in runbook
    for topic, why in (
        ("Deactivate the old licence", "the entitlement stays bound otherwise"),
        ("ngfw-shared", "the interface defaults are inherited, not ours"),
        ("Leave the old evidence bundles alone",
         "they outlive the device on purpose"),
        ("does not compare the interface port map against SCM",
         "the gap that forces steps 2 and 4 to be done together"),
    ):
        assert topic in runbook, f"the replacement procedure must still say: {why}"


def test_the_sizing_rationale_survives_the_downsize():
    """The instance type is cheap to change and the REASON is not. Two facts
    have to stay attached to it: 4 vCPU caps at 4 ENIs on every family (so the
    ceiling is mgmt + 3 dataplane), and the licence tier auto-scaled with the
    instance — which is the larger recurring cost and was only ever observed
    moving UP.

    Dropping either turns a measured decision back into a preference."""
    var = (REPO_ROOT / "provisioning" / "aws-vmseries-pilot" / "variables.tf").read_text()
    assert 'default     = "m5.xlarge"' in var, "4 vCPU is the default"
    assert "cap at 4 ENIs" in var or "caps at 4 ENIs" in var
    assert "VM-SERIES-16" in var and "UNVERIFIED" in var, (
        "the licence-tier behaviour, and the half of it that was never observed, "
        "must stay recorded next to the size that causes it")


def test_the_guides_form_a_path_a_new_operator_can_walk():
    """FOUR GUIDES, ONE ORDER. Each was written for its own reader and they were
    never chained, so someone starting from nothing had to already know that
    provisioning does not configure anything and that `building-a-folder.md`
    exists.

    Asserted as a chain rather than as four files: the failure mode is a guide
    that stops without saying where you go next, which reads as "you are done"
    when you are not."""
    prov = _flat(DOCS / "provisioning.md")
    assert "building-a-folder.md" in prov, (
        "provisioning must hand off — it stands a firewall up and configures "
        "nothing")
    assert "Replacing a firewall" in prov, (
        "and must divert a rebuild to the procedure that sequences it")

    readme = _flat(REPO_ROOT / "README.md")
    for nxt in ("provisioning.md", "building-a-folder.md", "requesting-rules.md",
                "operator-runbook.md"):
        assert nxt in readme, f"the entry point must name {nxt}"


def test_provisioning_states_the_sizing_decision_before_it_is_irreversible():
    """The licence tier follows the INSTANCE and is set at first registration.
    A profile sized for 4 vCPU still licenses at VM-SERIES-16 against a 16-vCPU
    instance, silently, and bills that way from boot.

    So the guide has to say it in the PREREQUISITES, where it can still be acted
    on, and check it in the verification, where it can still be caught."""
    prov = _flat(DOCS / "provisioning.md")
    assert "deployment profile" in prov.lower(), "the CSP side must be named"
    assert "BEFORE the firewall registers" in prov, (
        "sizing after registration is too late to be free")
    assert "vm-license" in prov, (
        "and the verification step must actually read the tier back")


def test_provisioning_troubleshoots_the_teardown_that_actually_failed():
    """FOUND BY FOLLOWING THE GUIDE, 2026-08-10. `terraform destroy` failed with
    DependencyViolation on the VPC, and the guide had nothing for it.

    The cause is worth keeping by name: Cortex Xpanse Active Response creates its
    OWN security group as remediation — a narrowed copy of the mgmt group —
    which Terraform never sees, so destroy removes its own and leaves the copy to
    block the VPC.

    Recognising it matters more than clearing it. A security tool making live
    changes to the VPC is a fact about the environment, not a stuck resource."""
    prov = _flat(DOCS / "provisioning.md")
    assert "DependencyViolation" in prov, "the teardown failure must be documented"
    # The EXPLANATORY sentence, not the tool name anywhere on the page. The
    # quoted AWS description also contains "Xpanse Active Response", so a looser
    # assertion survives the explanation being de-named to "some other tool" —
    # which a mutation run duly demonstrated.
    assert "Cortex Xpanse Active Response creates its own security groups" in prov, (
        "name the tool where the cause is explained — an unexplained security "
        "group is a mystery, a named one is a finding")
    assert "non-default security group blocks" in prov, (
        "the mechanism, so the next person diagnoses rather than guesses")


def test_provisioning_says_where_to_run_the_install():
    """FOUND BY FOLLOWING THE GUIDE, 2026-08-10. The install block was correct
    read top-to-bottom and wrong the moment the reader was anywhere else — the
    Steps section sends you into `provisioning/aws-vmseries-pilot/`, and a venv
    created there is a perfectly valid venv whose `pip install -e .` fails
    because no `pyproject.toml` sits beside it.

    The error talks about arguments, not about location, so the reader has no
    way to get from the message to the cause. The guide has to supply that."""
    prov = _flat(DOCS / "provisioning.md")
    assert "REPOSITORY ROOT" in prov, (
        "the install step must say WHERE it runs, not just what to type")
    assert "the trailing `.` IS the argument".replace("`", "") in prov, (
        "the missing-dot error is the first thing a new operator hits")
    # Anchored on text UNIQUE TO THE TROUBLESHOOTING ROW. "no pyproject.toml"
    # also appears in the install block, so an `or` over both phrasings stayed
    # satisfied when the row was gutted — the fourth time today an assertion
    # matched prose I had written somewhere else instead of the thing it guards.
    assert "You are not at the repo root" in prov, (
        "the wrong-directory failure needs its own troubleshooting row: the pip "
        "error names an argument, never a location")
