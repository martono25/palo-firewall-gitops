"""Python embedded in a workflow is code nobody imports and no test runs.

WHY THIS FILE EXISTS. `delete-scm-object.yml` deletes an object from SCM and
then writes the record of it. On its first real run, 2026-08-16, the delete
SUCCEEDED and the record step raised `NameError: name '_json' is not defined` —
an alias never imported. The pull-request step was skipped, so an irreversible
deletion left no evidence at all: the precise failure that workflow was built to
prevent. The same block read `os.environ["REASON"]` while the step declared only
KIND, FOLDER and NAME, so the reason — a REQUIRED input, documented as "lands in
the record" — would have landed empty even once the crash was fixed.

Neither defect is subtle. Both survived review, and 974 passing tests said
nothing, because a heredoc is a string as far as Python is concerned. These
tests give the strings the two cheapest checks the rest of the codebase gets for
free: every name is defined, and every environment variable is passed in.
"""

from __future__ import annotations

import ast
import builtins
import re
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: `python - <<'PY' ... PY`, the shape used throughout this repo.
_HEREDOC = re.compile(r"python -\s*<<'(\w+)'\n(.*?)\n\s*\1\b", re.DOTALL)

#: Always present in a GitHub runner, so not a missing declaration.
_AMBIENT_ENV = {"HOME", "PATH", "RUNNER_TEMP", "GITHUB_ENV", "GITHUB_OUTPUT",
                "GITHUB_WORKSPACE", "GITHUB_RUN_ID", "GITHUB_REPOSITORY",
                "GITHUB_SERVER_URL", "GITHUB_SHA", "GITHUB_REF", "GH_TOKEN"}


def _blocks():
    """Every embedded python block, with the step that carries it."""
    out = []
    for wf_path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text())
        job_envs = {}
        for job_name, job in (doc.get("jobs") or {}).items():
            job_envs[job_name] = job.get("env") or {}
            for step in job.get("steps") or []:
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for _, body in _HEREDOC.findall(run):
                    # No dedent: yaml.safe_load already strips the block
                    # scalar's indentation, so `run` holds the script at column
                    # zero. Stripping again silently ate ten real characters
                    # from every line and turned this into an IndentationError
                    # test.
                    src = textwrap.dedent(body)
                    out.append((wf_path.name, job_name,
                                step.get("name", "<unnamed>"), src,
                                set(step.get("env") or {}) | set(job_envs[job_name])))
    return out


BLOCKS = _blocks()


def test_there_are_embedded_blocks_to_check():
    """If the extractor silently matches nothing, every test below passes while
    checking exactly zero lines — which is how this whole class of defect got
    here in the first place."""
    assert BLOCKS, "no `python - <<'PY'` blocks found; the extractor is broken"
    assert any(w == "delete-scm-object.yml" for w, *_ in BLOCKS), (
        "the workflow whose crash prompted these tests must be among them")


@pytest.mark.parametrize("wf,job,step,src,env", BLOCKS,
                         ids=[f"{w}:{s}" for w, _, s, _, _ in BLOCKS])
def test_every_name_used_in_an_embedded_block_is_defined(wf, job, step, src, env):
    """The `_json` defect, caught statically.

    Not a full type check — just "is this name bound anywhere in the block, or a
    builtin". That is enough: `_json` and `_pathlib` were bound nowhere, and the
    only thing standing between that and a lost audit record was whether the
    line happened to execute.
    """
    tree = ast.parse(src, filename=f"{wf}:{step}")

    bound = set(dir(builtins)) | {"__name__", "__file__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            bound.update(a.arg for a in getattr(node.args, "args", []) or [])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)

    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    missing = sorted(used - bound)
    assert not missing, (
        f"{wf} step {step!r} uses undefined name(s) {missing}. This block runs "
        f"only in CI, often AFTER an irreversible action — `_json` cost the "
        f"record of a deletion on 2026-08-16.")


@pytest.mark.parametrize("wf,job,step,src,env", BLOCKS,
                         ids=[f"{w}:{s}" for w, _, s, _, _ in BLOCKS])
def test_every_env_var_a_block_reads_is_actually_passed_to_it(wf, job, step, src, env):
    """The silent half of the same failure.

    `os.environ.get("REASON", "")` does not raise when REASON was never declared
    — it returns "". The record would have been written, accepted, and empty in
    the field a human is required to fill in. A record that quietly omits WHY is
    worse than one that fails loudly.
    """
    tree = ast.parse(src, filename=f"{wf}:{step}")
    read = set()
    for node in ast.walk(tree):
        # os.environ["X"]
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            read.add(node.slice.value)
        # os.environ.get("X"[, default])
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get"
              and isinstance(node.func.value, ast.Attribute)
              and node.func.value.attr == "environ"
              and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            read.add(node.args[0].value)

    missing = sorted(read - env - _AMBIENT_ENV)
    assert not missing, (
        f"{wf} step {step!r} reads {missing} from the environment, but the step "
        f"does not pass them. `.get` returns \"\" rather than raising, so this "
        f"fails SILENTLY — REASON reached the deletion record empty this way.")
