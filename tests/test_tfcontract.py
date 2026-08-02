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
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")

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
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")

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
    assert payload["zones"]["dmz"]["network"] == {"layer3": ["ethernet1/2"]}


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
        (d / "ZONE.yaml").write_text(
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
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")
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
    (intent_dir / "ZONE.yaml").write_text(ZONE_INTENT, encoding="utf-8")
    env_map = tmp_path / "environments.yaml"
    env_map.write_text(ENV_MAP, encoding="utf-8")

    out = tmp_path / "terraform"
    rc = main([
        "compile", str(tmp_path / "intent"),
        "--env-map", str(env_map), "--out", str(out), "--allow-missing-root",
    ])

    assert rc == 0
    assert (out / "prod-edge" / "zones.auto.tfvars.json").exists()
