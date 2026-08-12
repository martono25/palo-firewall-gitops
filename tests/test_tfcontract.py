"""The compiler -> Terraform data contract.

These tests exist because the v1.0 suite could not have caught the bug that
shipped: every zone test asserted the compiler wrote the right JSON and stopped
there, which is precisely where the failure lived. Terraform then ignored the
file. So these assert the hop AFTER the compiler.

Two silent holes are covered, and the second is the quieter one:

  HOLE 1  key emitted, no `variable` declared   -> Terraform warns, exits 0
  HOLE 2  variable declared, never passed to the module -> NO diagnostic at all
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest



def _write_intent(directory, body, **_ignored):
    """Write an intent under the name its `metadata.id` requires.

    The product rejects a file whose name disagrees with its id (found live
    2026-08-11: `REQ-2026-0813.yaml` declared `REQ-2026-0812`, applied, and left
    the rule unfindable by the id a human would type). Fixtures used short names
    like `ZONE.yaml` for readability, which the rule now forbids — so the name is
    derived and callers stop choosing one.
    """
    import yaml as _y
    doc = _y.safe_load(body) or {}
    rid = (doc.get("metadata") or {}).get("id")
    p = directory / ((str(rid) + ".yaml") if rid else "REQ.yaml")
    p.write_text(body)
    return p
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fwgitops.cli import main  # noqa: E402
from fwgitops.tfcontract import (  # noqa: E402
    check_contract,
    declared_variables,
    is_terraform_root,
    module_arguments,
    wired_variables,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CLEAN_MODULE = '''
variable "folder" { type = string }
variable "zones"  { type = any }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
  zones  = var.zones
}
'''

# `zones` is declared but never passed to the module block. Terraform says
# NOTHING about this — quieter than the undeclared case.
UNWIRED_MODULE = '''
variable "folder" { type = string }
variable "zones"  { type = any }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
}
'''

UNDECLARED_MODULE = '''
variable "folder" { type = string }

module "security_folder" {
  source = "../modules/security_folder"

  folder = var.folder
}
'''


def _root(tmp_path: Path, body: str) -> Path:
    (tmp_path / "main.tf").write_text(body, encoding="utf-8")
    return tmp_path


# ── parsing ───────────────────────────────────────────────────────────────
def test_declared_variables_and_module_arguments(tmp_path):
    d = _root(tmp_path, CLEAN_MODULE)
    assert declared_variables(d) == {"folder", "zones"}
    assert {"folder", "zones"} <= module_arguments(d)


def test_commented_out_declaration_does_not_count(tmp_path):
    d = _root(tmp_path, '# variable "zones" { type = any }\nvariable "folder" {}\n')
    assert declared_variables(d) == {"folder"}


def test_nested_object_keys_are_not_module_arguments(tmp_path):
    """A key inside an object VALUE must not be mistaken for a module argument."""
    d = _root(
        tmp_path,
        '''
variable "folder" {}
module "m" {
  source = "../x"
  folder = var.folder
  settings = {
    zones = "not-a-module-argument"
  }
}
''',
    )
    args = module_arguments(d)
    assert "folder" in args and "settings" in args
    assert "zones" not in args


# ── the two holes ─────────────────────────────────────────────────────────
def test_clean_module_has_no_violations(tmp_path):
    assert check_contract(_root(tmp_path, CLEAN_MODULE), ["zones"]) == []


def test_hole_1_undeclared_variable_is_a_violation(tmp_path):
    problems = check_contract(_root(tmp_path, UNDECLARED_MODULE), ["zones"])
    assert len(problems) == 1
    assert "no `variable \"zones\"`" in problems[0]


def test_hole_2_declared_but_unwired_variable_is_a_violation(tmp_path):
    problems = check_contract(_root(tmp_path, UNWIRED_MODULE), ["zones"])
    assert len(problems) == 1
    assert "is never referenced" in problems[0]


def test_emitting_into_a_non_terraform_directory_is_a_violation(tmp_path):
    problems = check_contract(tmp_path, ["zones"])
    assert len(problems) == 1
    assert "no Terraform root module" in problems[0]


def test_no_emitted_keys_is_vacuously_fine(tmp_path):
    assert check_contract(tmp_path, []) == []
    assert not is_terraform_root(tmp_path)


# ── end-to-end: the compiler must refuse to write orphan data ─────────────
ENV_MAP = "prod:\n  folder: prod-edge\n  from_zone: local\n  to_zone: internet\n"

ZONE_INTENT = (
    "apiVersion: fw-intent/v1\n"
    "kind: ZoneRequest\n"
    "metadata: {id: ZONE-1, requester: m@corp, ticket: J-1, justification: dmz,"
    " requested: 2026-07-27}\n"
    "spec: {environment: prod, zone: dmz, type: layer3, interfaces: [ethernet1/2]}\n"
)


@pytest.mark.parametrize(
    "module_body,expected",
    [(UNDECLARED_MODULE, "no `variable"), (UNWIRED_MODULE, "is never referenced")],
)
def test_compile_refuses_to_write_orphan_tfvars(tmp_path, capsys, module_body, expected):
    """THE REGRESSION TEST for the v1.0 bug.

    A ZoneRequest compiled into a folder whose module cannot consume `zones`
    must FAIL and write nothing — not exit 0 having produced a file Terraform
    will ignore.
    """
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    _write_intent(intent_dir, ZONE_INTENT, encoding="utf-8")

    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    folder_dir = tmp_path / "terraform" / "prod-edge"
    folder_dir.mkdir(parents=True)
    (folder_dir / "main.tf").write_text(module_body, encoding="utf-8")

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map),
        "--out", str(tmp_path / "terraform"),
    ])

    assert rc == 2, "compile must fail closed, not silently emit ignored data"
    assert expected in capsys.readouterr().err
    assert not (folder_dir / "zones.auto.tfvars.json").exists(), "nothing may be written"


def test_compile_succeeds_when_the_module_consumes_zones(tmp_path):
    """Same intent, module wired correctly -> writes normally."""
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    _write_intent(intent_dir, ZONE_INTENT, encoding="utf-8")

    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    folder_dir = tmp_path / "terraform" / "prod-edge"
    folder_dir.mkdir(parents=True)
    (folder_dir / "main.tf").write_text(CLEAN_MODULE, encoding="utf-8")

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map),
        "--out", str(tmp_path / "terraform"),
    ])

    assert rc == 0
    payload = json.loads((folder_dir / "zones.auto.tfvars.json").read_text())
    assert payload["zones"]["dmz"]["network"]["layer3"] == ["ethernet1/2"]


# ── live repo contract ────────────────────────────────────────────────────
def test_this_repos_terraform_roots_satisfy_the_contract():
    """Guards the real tree: every root module must consume what it declares.

    Catches the next kind wired into the compiler but not into Terraform.
    """
    roots = [
        d
        for d in (REPO_ROOT / "terraform").iterdir()
        if d.is_dir()
        and is_terraform_root(d)
        and d.name not in {"modules"}
        and not d.name.startswith("bootstrap-")
        and d.name != "github-oidc"
    ]
    assert roots, "expected at least one Terraform root module"

    for root in roots:
        unwired = sorted(declared_variables(root) - wired_variables(root))
        assert not unwired, (
            f"{root.name}: variable(s) {unwired} declared but never passed to a module "
            f"block — Terraform gives no diagnostic and the value is silently ignored"
        )


# ── parser regressions (both bugs were real; verified by running them) ─────
def test_closing_brace_inside_a_string_does_not_fake_a_module_argument(tmp_path):
    """REGRESSION: a `}` inside a string collapsed the brace depth, so a key
    nested inside an object VALUE was counted as a top-level module argument.
    check_contract then PASSED a variable nothing wires — defeating HOLE 2,
    which is the entire reason this module exists."""
    d = _root(tmp_path, '''
variable "zones" { type = any }
module "m" {
  source = "../x"
  settings = {
    note  = "unbalanced } inside a string"
    zones = "nested, NOT a module argument"
  }
}
''')
    assert "zones" not in module_arguments(d)
    problems = check_contract(d, ["zones"])
    assert problems, "guard must not pass a variable nothing references"
    assert "is never referenced" in problems[0]


def test_double_slash_inside_a_url_does_not_eat_the_line(tmp_path):
    """REGRESSION: `//` in a URL was treated as a comment, truncating the line
    and deleting a closing brace with it. Depth stayed elevated, later arguments
    were skipped, and a CORRECTLY wired module was falsely rejected (exit 2)."""
    d = _root(tmp_path, '''
variable "zones" { type = any }
module "m" {
  source = "git::https://github.com/org/repo//modules/x?ref=v1"
  tags   = { url = "https://example.com" }
  zones  = var.zones
}
''')
    assert check_contract(d, ["zones"]) == []


def test_hash_inside_a_string_is_not_a_comment(tmp_path):
    d = _root(tmp_path, '''
variable "zones" { type = any }
module "m" {
  source = "../x"
  note   = "fragment#anchor"
  zones  = var.zones
}
''')
    assert check_contract(d, ["zones"]) == []


def test_var_reference_mentioned_only_inside_a_string_does_not_count(tmp_path):
    """A variable named in prose must not satisfy the wiring check."""
    d = _root(tmp_path, '''
variable "zones" { type = any }
module "m" {
  source = "../x"
  note   = "remember to pass var.zones one day"
}
''')
    assert check_contract(d, ["zones"]) != []


# ── wiring is about the VALUE reaching something, not argument naming ──────
def test_differently_named_module_argument_is_still_wired(tmp_path):
    d = _root(tmp_path, '''
variable "zones" { type = any }
module "m" {
  source    = "../x"
  zone_data = var.zones
}
''')
    assert check_contract(d, ["zones"]) == []


def test_variable_consumed_by_a_resource_not_a_module_is_wired(tmp_path):
    """A root need not use a module block at all to consume the value."""
    d = _root(tmp_path, '''
variable "zones" { type = any }
resource "scm_zone" "z" {
  for_each = var.zones
  name     = each.value.name
}
''')
    assert check_contract(d, ["zones"]) == []


# ── the shipped catalog must parse (symmetric with the repo-tree TF guard) ──
def test_the_shipped_env_map_parses_and_declares_its_baseline_zones():
    import yaml

    from fwgitops.resolve import EnvMap

    data = yaml.safe_load((REPO_ROOT / "catalog" / "environments.yaml").read_text())
    env_map = EnvMap.from_dict(data)
    baseline = env_map.baseline_zones_by_folder()["prod-edge"]
    # Verified live 2026-07-31: the folder carries seven zones.
    assert {"local", "internet", "proxy", "zone-internal"} <= baseline


# ── masking must never drop a line break (found by the ship coverage audit) ──
LINE_BREAK_CHARS = ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("brk", LINE_BREAK_CHARS)
def test_masking_preserves_every_line_break_character(brk):
    """THE invariant _strip_comments depends on.

    _mask_strings is a 1:1 character mapping, so line counts match only while
    every break character survives. Drop one and each later line pairs with the
    WRONG mask: in testing that truncated `module "n" {` to `mo` and discarded
    the trailing line, falsely rejecting a correctly wired module.
    """
    from fwgitops.tfcontract import _mask_strings
    text = f'module "m" {{\n  note = "a{brk}b"\n}}\n'
    assert len(_mask_strings(text).splitlines()) == len(text.splitlines())


def test_escaped_newline_in_a_string_does_not_drop_the_last_line(tmp_path):
    """REGRESSION: the escaped-character branch replaced `\\`+newline with "x",
    losing a line. zip() then truncated, dropping the wiring on the final line
    and FALSELY REJECTING a correct module."""
    d = _root(tmp_path, 'variable "zones" {}\nmodule "m" {\n  note = "abc\\\ndef"\n}\n'
                        'module "n" { zones = var.zones }\n')
    assert check_contract(d, ["zones"]) == []


def test_line_desync_does_not_corrupt_a_later_line(tmp_path):
    """REGRESSION: misalignment applied one line's comment offset to a DIFFERENT
    line, truncating `module "n" {` to `mo`. Code corruption, not just lost
    comments."""
    from fwgitops.tfcontract import _mask_strings, _strip_comments
    body = ('variable "zones" {}\nmodule "m" {\n  note = "x\\\ny"\n}\n'
            'module "n" {\n  # a comment\n  zones = var.zones\n}\n')
    assert 'module "n" {' in _strip_comments(body, _mask_strings(body)).splitlines()[5]
    assert check_contract(_root(tmp_path, body), ["zones"]) == []


def test_strip_comments_fails_safe_on_a_line_count_mismatch():
    """If the masker ever breaks its invariant again, return NOTHING.

    Returning the original looks conservative but is not: wired_variables reads
    the same text, so a surviving `# zones = var.zones` would count as a real
    reference and let HOLE 2 pass. Empty text makes every emitted key a HOLE 1
    violation instead — the compile is rejected rather than quietly weakened.
    """
    from fwgitops.tfcontract import _strip_comments
    assert _strip_comments("line one # cut me\nline two\nline three", "short") == ""


def test_a_real_double_slash_comment_is_still_cut(tmp_path):
    """The `//`-in-URL fix must not stop `//` working as an actual comment."""
    d = _root(tmp_path, '// variable "zones" { type = any }\nvariable "folder" {}\n')
    assert declared_variables(d) == {"folder"}


def test_escaped_quote_keeps_the_string_open(tmp_path):
    """`\\"` is not a terminator, so a `}` after it is still inside the string."""
    d = _root(tmp_path, 'variable "zones" {}\nmodule "m" {\n'
                        '  note = "she said \\"hi\\" and } stayed inside"\n'
                        '  zones = var.zones\n}\n')
    assert check_contract(d, ["zones"]) == []


def test_check_contract_reports_every_violation_not_just_the_first(tmp_path):
    d = _root(tmp_path, 'module "m" {\n  source = "../x"\n}\n')
    problems = check_contract(d, ["zones", "interfaces", "routes"])
    assert len(problems) == 3


def test_duplicate_emitted_keys_are_deduped(tmp_path):
    d = _root(tmp_path, UNDECLARED_MODULE)
    assert len(check_contract(d, ["zones", "zones", "zones"])) == 1


def test_a_bad_folder_blocks_the_good_folders_write(tmp_path, capsys):
    """All-or-nothing across folders.

    Correct today only because run_compile plans every file before writing any.
    A refactor moving the write loop above the contract loop would leave the
    rest of the suite green while silently writing a half-applied desired state.
    """
    for env, folder, zone in (("prod", "good-folder", "dmz"), ("stage", "bad-folder", "dmz2")):
        d = tmp_path / "intent" / env
        d.mkdir(parents=True)
        _write_intent(d, 
            "apiVersion: fw-intent/v1\nkind: ZoneRequest\n"
            f"metadata: {{id: Z-{env}, requester: m@corp, ticket: J-1,"
            " justification: x, requested: 2026-07-27}\n"
            f"spec: {{environment: {env}, zone: {zone}, type: layer3, interfaces: []}}\n",
            encoding="utf-8",
        )

    env_map = tmp_path / "environments.yaml"
    env_map.write_text(
        "prod:\n  folder: good-folder\n  from_zone: local\n  to_zone: internet\n"
        "stage:\n  folder: bad-folder\n  from_zone: local\n  to_zone: internet\n",
        encoding="utf-8",
    )

    good = tmp_path / "terraform" / "good-folder"
    good.mkdir(parents=True)
    (good / "main.tf").write_text(CLEAN_MODULE, encoding="utf-8")   # consumes zones
    bad = tmp_path / "terraform" / "bad-folder"
    bad.mkdir(parents=True)
    (bad / "main.tf").write_text(UNDECLARED_MODULE, encoding="utf-8")  # does not

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map),
        "--out", str(tmp_path / "terraform"),
    ])

    assert rc == 2
    assert "bad-folder" in capsys.readouterr().err
    assert not (bad / "zones.auto.tfvars.json").exists()
    assert not (good / "zones.auto.tfvars.json").exists(), \
        "a violation in ONE folder must block every folder's write"


# ── a MISSING Terraform root is itself a violation (red-team finding) ──────
def test_compile_rejects_a_folder_with_no_terraform_root(tmp_path, capsys):
    """REGRESSION: the fail-closed contract used to be fail-OPEN here.

    Gating the check on "does a Terraform root exist" made the check that
    catches missing Terraform skip precisely when Terraform was missing. Add an
    environment whose terraform/<folder>/ does not exist yet and compile passed;
    both CI loops then skipped the folder (`[ -f "$dir/main.tf" ] || continue`),
    so the plan and its undeclared-variable grep never ran either. Green PR,
    green apply, config never reaching the device.
    """
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    _write_intent(intent_dir, ZONE_INTENT, encoding="utf-8")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    out = tmp_path / "terraform"          # note: no prod-edge/ and no .tf anywhere
    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map), "--out", str(out),
    ])

    assert rc == 2, "a missing Terraform root must fail closed"
    assert "no Terraform root module" in capsys.readouterr().err
    assert not (out / "prod-edge" / "zones.auto.tfvars.json").exists()


def test_allow_missing_root_opts_out_explicitly(tmp_path):
    """Scratch/scaffold use stays possible, but only when asked for by name."""
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    _write_intent(intent_dir, ZONE_INTENT, encoding="utf-8")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    out = tmp_path / "terraform"
    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map), "--out", str(out), "--allow-missing-root",
    ])

    assert rc == 0
    assert (out / "prod-edge" / "zones.auto.tfvars.json").exists()


# ── HOLE 3: object ATTRIBUTES, not just top-level keys ────────────────────
V1_NARROW_ROOT = '''
variable "security_rules" {
  type = map(object({
    name    = string
    folder  = string
    action  = string
    log_end = bool
    tags    = list(string)
  }))
  default = {}
}
module "m" {
  source         = "../modules/security_folder"
  security_rules = var.security_rules
}
'''

RULE_PAYLOAD = {"r1": {
    "name": "x", "folder": "f", "action": "allow", "log_end": True, "tags": [],
    "application": ["ssl"], "profile_group": "best-practice", "log_setting": "log-best",
    "rulebase": "pre", "relative_position": "bottom", "target_rule": None,
}}


def test_hole_3_undeclared_object_attributes_are_caught(tmp_path):
    """THE v1.0 BUG. The root type omitted the six ADR-0003 attributes while the
    module declared them and the compiler emitted them. Terraform DISCARDS
    undeclared object attributes silently — no warning, exit 0 — so rules were
    built with application=["any"] instead of the intent's App-ID.

    Invisible to the key-level check: `security_rules` is declared AND wired.
    """
    from fwgitops.tfcontract import check_object_attributes
    d = _root(tmp_path, V1_NARROW_ROOT)
    assert check_contract(d, ["security_rules"]) == [], "key-level check cannot see this"
    problems = check_object_attributes(d, "security_rules", RULE_PAYLOAD)
    assert len(problems) == 1
    for attr in ("application", "profile_group", "log_setting",
                 "rulebase", "relative_position", "target_rule"):
        assert attr in problems[0]


def test_a_matching_object_type_has_no_attribute_violations(tmp_path):
    from fwgitops.tfcontract import check_object_attributes
    body = V1_NARROW_ROOT.replace(
        "    tags    = list(string)",
        """    tags    = list(string)
    application       = optional(list(string), ["any"])
    profile_group     = optional(string)
    log_setting       = optional(string)
    rulebase          = optional(string, "pre")
    relative_position = optional(string, "bottom")
    target_rule       = optional(string)""",
    )
    assert check_object_attributes(_root(tmp_path, body), "security_rules", RULE_PAYLOAD) == []


def test_nested_attributes_are_scoped_to_their_parent_path(tmp_path):
    """`optional(object({...}))` nests. Its attribute names belong to the NESTED
    type, so they must never appear as bare top-level names — but they must be
    visible as dotted paths, because HOLE 3 applies at any depth.
    """
    from fwgitops.tfcontract import declared_object_attributes
    d = _root(tmp_path, '''
variable "zones" {
  type = map(object({
    name   = string
    folder = string
    network = optional(object({
      layer3         = optional(list(string))
      must_not_count = optional(string)
    }))
  }))
}
''')
    paths = declared_object_attributes(d, "zones")
    assert {"name", "folder", "network"} <= paths
    assert "network.layer3" in paths and "network.must_not_count" in paths
    # the nested names must NOT masquerade as attributes of the parent
    assert "layer3" not in paths and "must_not_count" not in paths


@pytest.mark.parametrize("body,var", [
    ('variable "folder" { type = string }', "folder"),
    ('variable "z" { type = any }', "z"),
    ('variable "m" { type = map(string) }', "m"),
    ('variable "other" { type = string }', "absent"),
])
def test_non_object_or_absent_variables_have_no_attribute_contract(tmp_path, body, var):
    from fwgitops.tfcontract import declared_object_attributes
    assert declared_object_attributes(_root(tmp_path, body), var) is None


def test_a_variable_name_is_found_even_though_masking_blanks_quotes(tmp_path):
    """Regression on the lookup itself: the variable NAME lives inside quotes,
    which the mask blanks, so the declaration must be located in unmasked text
    while brace-matching runs on the masked view."""
    from fwgitops.tfcontract import declared_object_attributes
    d = _root(tmp_path, 'variable "security_rules" {\n'
                        '  type = map(object({\n    name = string\n  }))\n}\n')
    assert declared_object_attributes(d, "security_rules") == {"name"}


def test_compile_rejects_a_root_whose_object_type_drops_emitted_attributes(tmp_path, capsys):
    """End-to-end: HOLE 3 must fail the compile, not just the helper."""
    intent_dir = tmp_path / "intent" / "prod"
    intent_dir.mkdir(parents=True)
    _write_intent(intent_dir, ZONE_INTENT, encoding="utf-8")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    folder_dir = tmp_path / "terraform" / "prod-edge"
    folder_dir.mkdir(parents=True)
    # `zones` is declared and wired, but its object type omits `network`.
    (folder_dir / "main.tf").write_text('''
variable "zones" {
  type = map(object({
    name   = string
    folder = string
  }))
}
module "m" {
  source = "../modules/security_folder"
  zones  = var.zones
}
''', encoding="utf-8")

    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map), "--out", str(tmp_path / "terraform"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not declare attribute(s)" in err and "network" in err
    assert not (folder_dir / "zones.auto.tfvars.json").exists()


def test_this_repos_root_object_types_accept_everything_the_compiler_emits():
    """Guards the real tree against the root and module types drifting apart —
    which is exactly what happened in v1.0, under a comment claiming they were
    'kept in sync with compiler.py'.
    """
    import json
    import tempfile

    from fwgitops.cli import run_compile
    from fwgitops.tfcontract import check_object_attributes

    out = Path(tempfile.mkdtemp())
    rc = run_compile(REPO_ROOT / "intent", REPO_ROOT / "catalog" / "environments.yaml",
                     out, write=True, require_terraform_root=False,
                     out=open("/dev/null", "w"))
    assert rc == 0

    for emitted in out.rglob("*.auto.tfvars.json"):
        folder = emitted.parent.name
        root = REPO_ROOT / "terraform" / folder
        if not root.is_dir():
            continue
        payload = json.loads(emitted.read_text())
        for key, value in payload.items():
            assert check_object_attributes(root, key, value) == [], (
                f"{folder}: root variable {key!r} does not declare everything the "
                f"compiler emits — Terraform would discard it silently"
            )


# ── HOLE 3 at DEPTH (ADR-0005 prerequisite: `layer3` is a nested object) ────
def test_hole_3_is_caught_inside_a_nested_object(tmp_path):
    """The documented limit of the original check, now closed.

    Terraform discards an undeclared attribute at ANY depth. `layer3` on an
    interface and `network` on a zone are nested objects, so a root whose NESTED
    type is narrower than the module's would drop fields with the top-level key
    looking perfectly fine.
    """
    from fwgitops.tfcontract import check_object_attributes
    d = _root(tmp_path, '''
variable "zones" {
  type = map(object({
    name    = string
    network = optional(object({
      layer3 = optional(list(string))
    }))
  }))
}
module "m" {
  source = "../x"
  zones  = var.zones
}
''')
    payload = {"dmz": {"name": "dmz", "network": {
        "layer3": [], "zone_protection_profile": "best-practice"}}}
    problems = check_object_attributes(d, "zones", payload)
    assert len(problems) == 1
    assert "network.zone_protection_profile" in problems[0]


def test_a_nested_type_that_declares_everything_is_clean(tmp_path):
    from fwgitops.tfcontract import check_object_attributes
    d = _root(tmp_path, '''
variable "zones" {
  type = map(object({
    name    = string
    network = optional(object({
      layer3                  = optional(list(string))
      zone_protection_profile = optional(string)
    }))
  }))
}
module "m" { source = "../x"
  zones = var.zones }
''')
    payload = {"dmz": {"name": "dmz", "network": {
        "layer3": [], "zone_protection_profile": "best-practice"}}}
    assert check_object_attributes(d, "zones", payload) == []


def test_a_null_nested_object_asserts_nothing_about_its_children(tmp_path):
    """The compiler emits `"user_acl": null` for an unset optional object. That
    is a claim about `user_acl`, not about its nested attributes."""
    from fwgitops.tfcontract import check_object_attributes
    d = _root(tmp_path, '''
variable "zones" {
  type = map(object({
    name     = string
    user_acl = optional(object({ include_list = optional(list(string)) }))
  }))
}
module "m" { source = "../x"
  zones = var.zones }
''')
    assert check_object_attributes(d, "zones", {"dmz": {"name": "dmz", "user_acl": None}}) == []


def test_the_repos_real_zone_type_declares_every_nested_path_emitted():
    """Guards the live tree at depth, not just at the top level."""
    from fwgitops.compiler import CompiledZone, zone_tfvars
    from fwgitops.tfcontract import check_object_attributes
    z = CompiledZone(folder="prod-edge", name="dmz", zone_type="layer3", interfaces=[],
                     protection_profile="p", log_forwarding="l", user_id=True,
                     user_acl={"include_list": [], "exclude_list": []},
                     device_acl={"include_list": [], "exclude_list": []})
    root = REPO_ROOT / "terraform" / "prod-edge"
    assert check_object_attributes(root, "zones", zone_tfvars([z])["zones"]) == []


def test_no_compiled_tfvars_artifact_is_tracked_by_git():
    """Compiled desired-state is a BUILD ARTIFACT, never source.

    A committed `*.auto.tfvars.json` can go stale against the intent it claims to
    represent, and CI recompiles from intent on every run — so a tracked one is
    silently ignored at best and misleading at worst. The gitignore uses a glob
    rather than one line per kind precisely so adding a kind cannot forget it.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "*.auto.tfvars.json"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert tracked == [], (
        f"compiled tfvars committed as source: {tracked}. These are build "
        f"artifacts — check the `terraform/*/*.auto.tfvars.json` gitignore glob."
    )
