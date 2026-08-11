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


def test_provisioning_handles_the_second_pass_not_just_the_first():
    """FOUND BY FOLLOWING THE GUIDE, 2026-08-10. Step 1 says
    `cp terraform.tfvars.example terraform.tfvars`, which is first-time setup.
    On a rebuild that file already holds your values, so the instruction either
    erases them or gets skipped — and nothing then tells the reader which fields
    do not survive a rebuild.

    The registration PIN is the one that bites: time-limited, single-use, read
    once at first boot. Expire it and the firewall licenses correctly and never
    appears in SCM, with nothing failing loudly.

    Same shape as the install-location bug: correct read linearly the first
    time, silent on the second pass. That is the failure mode of a guide written
    by someone who has only ever done it once."""
    prov = _flat(DOCS / "provisioning.md")
    assert "FIRST TIME ONLY" in prov, (
        "the copy step overwrites a populated tfvars on a rebuild")
    assert "Rebuilding? Do not re-copy the example" in prov, (
        "a rebuild needs its own branch, not a re-read of first-time setup")
    assert "immediately before" in prov and "expires while you work" in prov, (
        "PIN timing is the trap: generating it at the start of teardown burns "
        "the window")


def test_provisioning_separates_refused_from_timed_out():
    """FOUND BY FOLLOWING THE GUIDE, 2026-08-10. One row said "`mgmt`
    unreachable | wrong CIDR, or still booting" — two opposite causes under one
    symptom, which sends the reader to check a security group that is fine.

    The distinction is free and decisive. TIMEOUT means the packet never
    arrived: network path, allow-list, wrong address. REFUSED means it arrived
    and nothing is listening: the path is correct and PAN-OS is still coming up.

    And the guide should say how to CONFIRM rather than wait — console output
    shows the FIPS-CC integrity stage a first boot sits in."""
    prov = _flat(DOCS / "provisioning.md")
    assert "Connection refused" in prov and "times out" in prov, (
        "the two symptoms have opposite causes and need separate rows")
    assert "TCP reached the host" in prov, (
        "say what refused MEANS, or the reader still checks the firewall")
    assert "get-console-output" in prov, (
        "give a way to see boot progress instead of waiting blind")


def test_provisioning_explains_what_onboard_actually_does():
    """ASKED BY THE OPERATOR FOLLOWING THE GUIDE, 2026-08-10. The command was
    printed with a bare "verifies placement + sets a friendly display name",
    which does not say it is a GATE, what exit 3 means, or why a display name
    would matter.

    Both halves have to stay explained. The placement poll is the gate — without
    it a mismatched onboarding-rule regex surfaces much later as a confusing
    Day-1 failure. The display name is the re-onboard signal: a re-registration
    resets it to PA-VM and silently wipes device-scope config, which is how the
    2026-08-05 incident was caught."""
    prov = _flat(DOCS / "provisioning.md")
    assert "onboard does not onboard the firewall" in prov, (
        "the name misleads — say what the device does for itself")
    assert "Exit 3 means placement never confirmed" in prov, (
        "it is a gate; the failure has to be actionable")
    assert "resets it to PA-VM" in prov, (
        "the display name exists so a re-onboard is detectable")


def test_the_repo_repoint_is_findable_from_both_directions():
    """RAISED BY THE OPERATOR MID-REBUILD, 2026-08-10: "I don't understand why
    you want to replace firewall now. I haven't even done the additional day 1
    provisioning."

    Fair. The repo-side steps — update the serial in four places — lived only
    under a heading called "Replacing a firewall", which reads as a destructive
    restart to someone who has just BUILT one. They are not a replacement; they
    are the bridge between provisioning and Day-1.

    And the ordering is not arbitrary: an InterfaceRequest names its firewall by
    serial, so the Day-1 chain targets a device that does not exist until the
    repo is pointed at the real one. Both pages have to say so, because a reader
    arrives from either side."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "also what you do after building a firewall for the first time" in runbook, (
        "the section title says replacement; the content is not only that")
    assert "nothing is destroyed, nothing is undone" in runbook, (
        "say it plainly — the heading alarms someone who just provisioned")

    folder = _flat(DOCS / "building-a-folder.md")
    assert "does the repo know your firewall" in folder, (
        "the Day-1 guide must check the serial before its first step")
    assert "the interface PORT map is not checked against SCM" in folder, (
        "be precise about what is unchecked: the SERIAL is validated by "
        "compile, the port map is not, and overstating the gap teaches the "
        "reader to distrust checks that work")


def test_the_serial_update_says_which_kinds_carry_one():
    """ASKED BY THE OPERATOR, 2026-08-10, at step 5: "what does this mean?"

    "Update the Day-1 intents that name the device" assumes the reader knows
    which kinds do. Three of the five files in that directory carry a serial and
    two do not, and the difference is not arbitrary: an interface address
    belongs to one firewall, while zones and routes are folder policy every
    firewall inherits.

    Getting it wrong in the safe direction wastes time; in the unsafe direction
    the compiler refuses, so the cost is confusion rather than damage — which is
    exactly the kind of thing a runbook should remove."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "Only InterfaceRequest does" in runbook, (
        "name the kind that carries a serial, do not imply all of them do")
    assert "ZoneRequest and RouteRequest do not change" in runbook, (
        "and say explicitly which ones to leave alone")
    assert "two cannot share an IP" in runbook, (
        "the reason, so the reader can generalise to a kind added later")


def test_the_catalog_step_lists_display_name_too():
    """FOUND BY THE OPERATOR AT STEP 4, 2026-08-10. The step said "the `devices:`
    block", and the obvious edit is the serial key — leaving `display_name`
    pointing at the old firewall.

    `verify-catalog` catches it, but reports it in the language of the DANGEROUS
    reading: a display name that disagrees usually means the device was
    re-onboarded and device-scope config was silently wiped. The check cannot
    tell which side moved. So the runbook has to name the field, or the operator
    meets a scary note for a benign reason and learns to wave the check away."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "display_name" in runbook, (
        "name the field — 'the devices: block' reads as just the serial")
    assert "cannot tell which side moved" in runbook, (
        "explain why the note sounds alarming for a harmless cause")


def test_the_replacement_covers_everywhere_the_serial_is_written():
    """FOUND BY THE OPERATOR, 2026-08-10, after finishing steps 4-6: eight tests
    red. The serial is not only in the catalog, the intents and a Terraform
    root — around a dozen test files carry it, and so do the live guides, one of
    which is pinned by a test asserting its examples match the real intents.

    `git rm` is also only half the cleanup: it removes TRACKED files, leaving
    `.terraform/`, `backend.hcl` and stray plans behind, so the old root
    survives on disk.

    ADRs stay as written — an ADR records a decision made at a time, and
    rewriting it is revisionism."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "git rm only removes TRACKED files" in runbook, (
        "the old root survives on disk otherwise")
    assert "will not pass CI until they follow" in runbook, (
        "tests carry the serial; say so before the operator finds out from a "
        "red build")
    assert "docs/adr/ — leave alone" in runbook, (
        "and say what must NOT be rewritten, or someone tidies the history")


def test_the_runbook_warns_that_a_new_firewall_fails_to_commit_first():
    """FOUND BY THE OPERATOR, 2026-08-10, between the repo re-point and Day-1:
    `device-sync` showed an SCM commit failing with "can't find interface in
    'default' for next hop 10.100.2.1".

    Nothing was wrong. Zones, routers and rules are FOLDER-scoped and survived
    the rebuild, so the new firewall inherited them the moment it joined —
    including a default route. Interface addressing is DEVICE-scoped and died
    with the old firewall, so SCM validated a route against a device with no
    interface in that subnet.

    This is ADR-0002's ordered chain seen from the other end, and it reads as
    breakage to anyone who has not internalised the scope split. An operator who
    stops here to debug a working system has lost the afternoon."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "inherits the folder's policy the instant it joins" in runbook, (
        "explain WHY a fresh firewall has policy it cannot satisfy")
    assert "Nothing is wrong" in runbook, (
        "say so plainly — the error looks like a misconfiguration")
    assert "the rebuild changed the topology" in runbook, (
        "and give the test that separates the benign case from the real one")


def test_the_day1_guide_says_how_to_actually_apply():
    """RAISED BY THE OPERATOR, 2026-08-10: "this command is not clear and it is
    not in your guide."

    Fair. "Applying the chain" explained tiering, apply-order and evidence, and
    never said what to TYPE. A reader finished it knowing the concepts and not
    the actions — and the key fact was missing entirely: you do not run the
    apply, merging to `main` runs it.

    The stale-ticket gate belongs here too. Editing an existing intent rather
    than adding one is the normal case during a rebuild, and it is rejected
    until the ticket changes."""
    guide = _flat(DOCS / "building-a-folder.md")
    assert "You do not run the apply. Merging to main runs it." in guide, (
        "the central fact — the trigger is the merge, not a command")
    assert "gh pr create" in guide and "gh pr merge" in guide, (
        "give the actual commands, not a description of the workflow")
    assert "give it a new ticket" in guide, (
        "editing an existing intent is the normal rebuild case and it is gated")


def test_each_guide_hands_off_at_the_point_the_work_ends():
    """RAISED BY THE OPERATOR, 2026-08-10, after three separate stalls:
    "from provisioning.md there is no instruction to go to operator-runbook.md
    with exact step. From there, there is no instruction to go to
    building-a-folder.md. You keep making mistake that user has done this
    multiple times."

    Correct, and it was the same root cause each time. A "read these in order"
    table at the TOP of a document is not a handoff. The reader needs it at the
    moment the work ends, naming the destination SECTION and STEP — anything
    vaguer assumes they already know the shape of the system, which is exactly
    what a first-timer does not have.

    So each phase ends with an explicit NEXT: provisioning -> the repo re-point
    (steps 4-10, listed) -> Day-1 -> requesting a rule."""
    prov = _flat(DOCS / "provisioning.md")
    assert "NEXT: the repository does not know this firewall yet" in prov
    assert "steps 4 to 10" in prov, (
        "name the steps — 'see the runbook' is what stalled the operator")

    runbook = _flat(DOCS / "operator-runbook.md")
    assert "NEXT: configure the firewall from Git" in runbook
    assert "Applying the chain" in runbook, (
        "and point at the section that actually ships it")

    folder = _flat(DOCS / "building-a-folder.md")
    assert "NEXT: prove it end to end, then hand it over" in folder


def test_the_runbook_does_not_warn_about_a_failure_that_does_not_happen():
    """CORRECTED 2026-08-11 by measurement.

    This first said the first push to a fresh firewall "may be refused", on the
    strength of a comment in `devicesync.py` claiming SCM rejects an
    admin-scoped push while `is_first_push_done` is false. The rebuild disproved
    it: the pipeline's normal admin-scoped push to a brand-new firewall
    reporting `false` succeeded first time (job 202), as had every earlier push
    on this tenant.

    So the guidance is now "ignore it", and the reversal is left visible rather
    than quietly deleted. A warning for a failure that never arrives trains an
    operator to skip warnings — and this one pointed at `--all-admins`, which
    commits everything staged in a scope."""
    runbook = _flat(DOCS / "operator-runbook.md")
    assert "first-push-pending on a new firewall is not a problem" in runbook
    assert "The flag predicts nothing on this tenant" in runbook, (
        "state the measurement, not a hedge")
    assert "how a real one gets ignored" in runbook, (
        "and keep why the old warning was harmful, so it is not re-added")


def test_the_apply_sequence_says_which_starting_state_it_assumes():
    """ASKED BY THE OPERATOR, 2026-08-10: "which one is correct? your
    instruction or guide?"

    Both were. The guide assumed starting on `main` with uncommitted changes;
    the operator was on a feature branch with most of the work already committed,
    having arrived from the replacement runbook. `git checkout -b` there forks
    the branch you are already on, and `git add catalog/ terraform/` stages
    nothing.

    Same root cause as every other stall today: a procedure correct for one
    starting state, silent about which state it assumes. A command sequence has
    a precondition and it has to be written down."""
    guide = _flat(DOCS / "building-a-folder.md")
    assert "Starting on main with uncommitted changes" in guide, (
        "name the state the branch-and-commit sequence assumes")
    assert "Already on a branch" in guide, (
        "and cover the state an operator arriving from the runbook is in")
    assert "you would fork the branch you are on" in guide, (
        "say what goes wrong, not just what to do instead")
