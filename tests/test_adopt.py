"""Adopting a firewall reads SCM; it does not ask you to type what SCM knows.

Seventeen hand edits across two catalogs, three intents, a directory name and a
Terraform root — every one transcribing a value SCM already held. The operator
who walked it: "too many manual task to edit i.e. folders, device scope and rules
which is prone to error and typo."

These tests are about the READ and the REFUSALS. A value read from SCM cannot
disagree with SCM, which is also why this closes the gap where nothing compared
`catalog/interfaces.yaml` to the live tenant.
"""

from __future__ import annotations

import pytest

from fwgitops.adopt import AdoptError, plan_adoption

ROLES = {"local": "$eth-local", "internet": "$eth-internet", "dmz": "$eth-dmz"}


class FakeScm:
    def __init__(self, folder="prod-edge", name="fw-prod-edge-1881", variables=None):
        self._folder, self._name = folder, name
        self._vars = variables if variables is not None else {
            "$eth-local": "ethernet1/1",
            "$eth-internet": "ethernet1/2",
            "$eth-dmz": "ethernet1/3",
        }

    def device_folder(self, serial):
        return self._folder

    def device_display_name(self, serial):
        return self._name

    def folder_interface_variables(self, folder):
        return dict(self._vars)


def test_the_port_map_comes_from_SCM_not_from_typing():
    """The whole point. `catalog/interfaces.yaml` is a mirror, and nothing ever
    compared it to the tenant — a wrong port compiled clean and configured the
    wrong interface. Reading it removes both the typo and the drift."""
    a = plan_adoption(FakeScm(), "007955000902404", folder="prod-edge", roles=ROLES)
    assert a.ports == {"local": "ethernet1/1", "internet": "ethernet1/2",
                       "dmz": "ethernet1/3"}
    assert a.display_name == "fw-prod-edge-1881"
    assert a.unresolved == []


def test_a_device_in_a_different_folder_is_refused():
    """Writing the folder you MEANT would make the catalog assert a placement
    that is not real — and every later check, `verify-catalog` included, trusts
    the catalog. The failure names both folders so the operator can tell which
    one is wrong."""
    with pytest.raises(AdoptError, match="is in folder 'ngfw-shared', not 'prod-edge'"):
        plan_adoption(FakeScm(folder="ngfw-shared"), "0079", folder="prod-edge",
                      roles=ROLES)


def test_an_unregistered_device_is_refused_with_where_to_look():
    """A serial SCM has never seen is the common case when someone adopts too
    early — the firewall is still booting, or the onboarding rule did not match.
    Both are actionable, so the message says both."""
    with pytest.raises(AdoptError, match="did not match it"):
        plan_adoption(FakeScm(folder=None), "0079", folder="prod-edge", roles=ROLES)


def test_a_role_with_no_variable_is_reported_not_guessed():
    """A role SCM has no variable for is a role with no port. Defaulting it would
    write a plausible wrong answer into the one file nothing validates."""
    a = plan_adoption(FakeScm(variables={"$eth-local": "ethernet1/1"}),
                      "0079", folder="prod-edge", roles=ROLES)
    assert a.ports == {"local": "ethernet1/1"}
    assert a.unresolved == ["dmz", "internet"], (
        "the roles that did not resolve must be reported, so the operator sees "
        "a partial adoption rather than assuming a complete one")


def test_no_resolvable_role_at_all_is_a_hard_failure():
    """A port map with nothing in it cannot compile any intent naming a role, so
    writing the catalog would produce a repository that looks adopted and is
    not."""
    with pytest.raises(AdoptError, match="nothing useful to write"):
        plan_adoption(FakeScm(variables={}), "0079", folder="prod-edge", roles=ROLES)


def test_planning_touches_nothing():
    """`plan_adoption` is a read. The writer is separate and takes this result,
    so `--check` is the same code path as the real thing minus the write —
    rather than a second implementation that can disagree with it."""
    import inspect
    from fwgitops import adopt
    src = inspect.getsource(adopt)
    for forbidden in ("write_text(", "open(", "PUT", "POST", "DELETE"):
        assert forbidden not in src, (
            f"the planner must not {forbidden} — it reads SCM and returns a plan")


# ── applying the plan ──────────────────────────────────────────────────────
from fwgitops.adopt import Adoption, apply_adoption   # noqa: E402

INTERFACES = '''# A long comment explaining WHY this mapping is what it is.
interfaces:
  local:
    description: Inside.
    folder: $eth-local
    # No `create_in`: an SCM default. NOT ours.
    devices:
      "OLD-1": ethernet1/4

  internet:
    folder: $eth-internet
    devices:
      "OLD-1": ethernet1/3
'''

FOLDERS = '''folders:
  prod-edge:
    devices:
      "OLD-1":
        # A comment that must survive.
        display_name: fw-prod-edge-old
        model: PA-VM
        targetable: true
'''


def _adoption(**kw):
    base = dict(serial="NEW-2", folder="prod-edge", display_name="fw-prod-edge-new",
                ports={"local": "ethernet1/1", "internet": "ethernet1/2"},
                unresolved=[])
    return Adoption(**{**base, **kw})


def test_the_serial_is_replaced_everywhere_it_appears():
    """The seventeen edits, in one operation. A PARTIAL rename is the failure
    this command exists to remove — the catalog updated and an intent left
    behind is a tree that compiles and targets a device that does not exist."""
    out = apply_adoption(_adoption(), folders_text=FOLDERS, interfaces_text=INTERFACES,
                         intent_files={"intent/prod/f/REQ-1.yaml": 'spec:\n  device: "OLD-1"\n'},
                         replacing="OLD-1")
    assert "NEW-2" in out["intent/prod/f/REQ-1.yaml"]
    assert "OLD-1" not in out["intent/prod/f/REQ-1.yaml"]
    assert "OLD-1" not in out["catalog/folders.yaml"]


def test_the_comments_survive():
    """`catalog/interfaces.yaml` is mostly comments explaining which ENI sits
    behind which port and why a role is site-specific. Round-tripping it through
    a YAML parser would delete all of it, and those comments are the only reason
    the file is followable."""
    out = apply_adoption(_adoption(), folders_text=FOLDERS, interfaces_text=INTERFACES,
                         intent_files={}, replacing="OLD-1")
    assert "explaining WHY this mapping" in out["catalog/interfaces.yaml"]
    assert "an SCM default. NOT ours." in out["catalog/interfaces.yaml"]
    assert "A comment that must survive." in out["catalog/folders.yaml"]


def test_the_ports_are_set_per_role_not_globally():
    """Every role has a `devices:` map. A global replace would rewrite them all
    to one port — which is exactly the silent wrong-interface bug the catalog
    read exists to prevent, reintroduced by the writer."""
    out = apply_adoption(_adoption(), folders_text=FOLDERS, interfaces_text=INTERFACES,
                         intent_files={}, replacing="OLD-1")
    text = out["catalog/interfaces.yaml"]
    assert '"NEW-2": ethernet1/1' in text, "local"
    assert '"NEW-2": ethernet1/2' in text, "internet"
    assert text.count("ethernet1/1") == 1 and text.count("ethernet1/2") == 1


def test_the_display_name_follows_SCM():
    """A stale one makes `verify-catalog` report a note worded for the DANGEROUS
    cause — a re-onboard that wipes device-scope config — when the truth is an
    un-updated catalog. Reading it removes the ambiguity at the source."""
    out = apply_adoption(_adoption(), folders_text=FOLDERS, interfaces_text=INTERFACES,
                         intent_files={}, replacing="OLD-1")
    assert "display_name: fw-prod-edge-new" in out["catalog/folders.yaml"]


def test_a_re_run_with_no_serial_change_still_corrects_a_drifted_port():
    """Adoption is not only for a new serial. The port map is authoritative from
    SCM, so re-running against the SAME device is how a catalog that has drifted
    from the tenant gets corrected — the gap where nothing compared the two."""
    drifted = INTERFACES.replace('"OLD-1": ethernet1/4', '"OLD-1": ethernet1/9')
    out = apply_adoption(_adoption(serial="OLD-1"), folders_text=FOLDERS,
                         interfaces_text=drifted, intent_files={})
    assert '"OLD-1": ethernet1/1' in out["catalog/interfaces.yaml"]
    assert "ethernet1/9" not in out["catalog/interfaces.yaml"]


def test_an_adoption_that_changes_nothing_writes_nothing():
    """FOUND ON THE FIRST LIVE RUN. The catalog already matched SCM exactly, and
    the command still reported a file to write — it was deleting two blank lines
    and nothing else, because the entry regex swallowed the newline after the
    value.

    A tool that silently reformats the file it edits is one people stop trusting,
    and it makes a real change impossible to see in the diff. `--check` reporting
    a no-op change also destroys the only signal it exists to give."""
    already = INTERFACES.replace('"OLD-1": ethernet1/4', '"OLD-1": ethernet1/1') \
                        .replace('"OLD-1": ethernet1/3', '"OLD-1": ethernet1/2')
    out = apply_adoption(_adoption(serial="OLD-1", display_name=None),
                         folders_text=FOLDERS, interfaces_text=already,
                         intent_files={})
    assert out == {}, f"a matching catalog must produce no changes, got {list(out)}"


def test_the_blank_lines_between_roles_survive():
    """The same bug, asserted on the shape rather than the outcome — the file is
    readable because its roles are separated, and an edit that closes them up
    degrades it a little on every run."""
    out = apply_adoption(_adoption(serial="OLD-1"), folders_text=FOLDERS,
                         interfaces_text=INTERFACES, intent_files={})
    text = out["catalog/interfaces.yaml"]
    assert text.count("\n\n") == INTERFACES.count("\n\n"), (
        "the blank lines separating roles must be preserved exactly")


# ── following the serial into the files that only break CI ─────────────────
from fwgitops.adopt import NEVER_FOLLOW, follow_serial   # noqa: E402


def test_the_serial_is_followed_into_tests_and_guides():
    """Seventy-six references across seventeen files on this deployment. They do
    not change behaviour — fixtures and prose — but they break CI, so an operator
    who has done everything else right still opens a pull request that cannot
    merge. That was the last manual step of the seventeen."""
    out = follow_serial("OLD-1", "NEW-2", {
        "tests/test_kinds.py": 'serial = "OLD-1"',
        "docs/building-a-folder.md": "the pilot OLD-1 was built",
        "docs/cli-reference.md": "fwgitops push --device OLD-1",
    })
    assert set(out) == {"tests/test_kinds.py", "docs/building-a-folder.md",
                        "docs/cli-reference.md"}
    assert "NEW-2" in out["tests/test_kinds.py"] and "OLD-1" not in out["tests/test_kinds.py"]


def test_ADRs_and_evidence_are_never_rewritten():
    """An ADR records a decision made at a time; rewriting one is revisionism.
    An evidence bundle is the audit trail of a change that really happened on a
    firewall that really existed, and a rebuild does not un-happen it.

    This is the exclusion most likely to be lost to a well-meaning "follow the
    serial everywhere" refactor, so it is asserted rather than commented."""
    out = follow_serial("OLD-1", "NEW-2", {
        "docs/adr/0005-interfacerequest-folder-scope.md": "targeted OLD-1",
        "evidence/device-OLD-1/REQ-1.json": '{"serial": "OLD-1"}',
        "tests/test_kinds.py": 'serial = "OLD-1"',
    })
    assert set(out) == {"tests/test_kinds.py"}, (
        f"only supporting files may follow the serial; got {sorted(out)}")
    for skip in NEVER_FOLLOW:
        assert not any(p.startswith(skip) for p in out)


def test_a_file_without_the_serial_is_left_alone():
    """Rewriting every file in `tests/` and `docs/` would produce a diff nobody
    can review, which is how a real change hides."""
    assert follow_serial("OLD-1", "NEW-2", {"tests/x.py": "nothing here"}) == {}


# ── what the command does, and the one thing it refuses to ─────────────────
def test_state_deletion_is_opt_in_and_everything_else_is_not():
    """FIVE STEPS WERE LEFT MANUAL; FOUR ARE NOW AUTOMATIC. Scaffolding the new
    root, removing the old one, and following the serial through `tests/` and the
    guides are all local, reversible and mechanical — exactly the kind of work a
    command should absorb.

    DELETING TERRAFORM STATE IS NOT. It is irreversible and REMOTE: the
    difference between "this command edits my repository" and "this command
    reaches into my cloud account and destroys a record". So it is `--prune-state`
    rather than a default, and without the flag the command prints the one-liner.

    The bucket is read from a backend.hcl rather than guessed — deleting from the
    wrong bucket is not a mistake worth risking to save a flag."""
    import inspect
    from fwgitops.cli import run_adopt_device, _state_bucket
    sig = inspect.signature(run_adopt_device).parameters
    assert "prune_state" in sig and sig["prune_state"].default is False, (
        "state deletion must be opt-in")

    src = inspect.getsource(run_adopt_device)
    assert "run_scaffold_root" in src, "the new Terraform root is scaffolded"
    assert "shutil.rmtree" in src, "the old root is removed, gitignored files included"
    assert "follow_serial" in src, "the serial is followed into tests/ and docs/"
    assert "irreversible" in src, "and the one exception says why it is one"

    assert "return None" in inspect.getsource(_state_bucket), (
        "an unreadable backend must yield None rather than a guessed bucket")


def test_the_root_work_does_not_depend_on_the_catalog_changing():
    """A device can be correctly declared and still have NO Terraform root —
    which was the bug in the first version: the command returned "nothing to
    change" before it ever looked, so adopting a device whose catalog already
    matched left it without a root.

    `scaffold-root` also refuses an existing root on purpose (main.tf is written
    once), so this asks whether the root exists rather than calling it and
    treating the refusal as a failure."""
    import inspect
    from fwgitops.cli import run_adopt_device
    src = inspect.getsource(run_adopt_device)
    assert "not changes and not root_work" in src, (
        "the early return must consider the roots, not only the catalog")
    assert "if not new_root.exists():" in src, (
        "scaffold-root refuses an existing root; ask before calling it")


def test_a_replacement_re_tickets_the_intents_it_changes():
    """FOUND BY USING THE COMMAND. It wrote twenty-two files correctly and
    produced a pull request that could not merge — failing the stale-ticket gate
    its own edit had triggered.

    A `spec` that changed while `metadata.ticket` did not is rejected, because
    the evidence bundle would otherwise name the request that authorised the
    PREVIOUS version. A replacement changes `spec.device` on every device-scoped
    intent, so an adoption trips that gate on every file it touches.

    Automating the edit and leaving the authorisation is half an answer."""
    body = ('apiVersion: fw-intent/v1\nkind: InterfaceRequest\nmetadata:\n'
            '  id: REQ-1\n  ticket: JIRA-OLD\n  requested: "2026-01-01"\n'
            'spec:\n  device: "OLD-1"\n')
    out = apply_adoption(_adoption(), folders_text=FOLDERS,
                         interfaces_text=INTERFACES,
                         intent_files={"intent/prod/f/REQ-1.yaml": body},
                         replacing="OLD-1", ticket="JIRA-NEW", today="2026-08-12")
    got = out["intent/prod/f/REQ-1.yaml"]
    assert "ticket: JIRA-NEW" in got and "JIRA-OLD" not in got
    assert 'requested: "2026-08-12"' in got, (
        "the date must move with the ticket — `requested` describes THIS change")
    assert '"NEW-2"' in got, "and the serial still changes"


def test_without_a_ticket_the_intents_are_left_alone():
    """`--ticket` is optional, because an adoption that replaces nothing changes
    no `spec` and needs no new authorisation. Re-ticketing regardless would
    rewrite a ticket for a change that did not happen."""
    body = ('metadata:\n  id: REQ-1\n  ticket: JIRA-OLD\n  requested: "2026-01-01"\n'
            'spec:\n  device: "OLD-1"\n')
    out = apply_adoption(_adoption(), folders_text=FOLDERS,
                         interfaces_text=INTERFACES,
                         intent_files={"intent/prod/f/REQ-1.yaml": body},
                         replacing="OLD-1")
    assert "ticket: JIRA-OLD" in out["intent/prod/f/REQ-1.yaml"]
