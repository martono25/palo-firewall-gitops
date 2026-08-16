"""Rule order is policy, and the policy is: deployment order.

WHY THIS EXISTS. Nothing detected a reorder and nothing healed one. Confirmed
2026-08-16 three ways: `drift.py` had no order logic at all, `terraform plan`
sees `relative_position = null` on every rule so there is nothing to diff, and
`enrich` deliberately skips rules whose position is unspecified. Order IS the
policy — a permissive rule moved above a restrictive one changes what traffic
passes without editing a single rule — and it was the one part of the policy
nobody was watching.

WHAT THE DECLARED ORDER IS, and why it needs no file. An intent carries no
position: a new rule lands at the bottom, which is the whole convention. So the
expected order is not a choice anybody makes, it is a CONSEQUENCE — rules appear
in the order they were deployed. That is already recorded in Git as the commit
that first added each intent, so there is nothing on disk to maintain and
nothing for anyone to edit. A manifest was considered and was not needed.

WHY NOT THE EVIDENCE BUNDLE'S TIMESTAMP, which is the obvious objection —
git records when an intent was MERGED, and what actually matters is when the
rule was APPLIED. The bundle looks like it knows. It does not: bundles are
regenerated on every apply, so `generated_at` records the LAST one. Measured
2026-08-16, all six of the pilot's rules read

    generated_at = 2026-08-15T14:22:17Z

identical to the second, while their commit times span 26 Jul to 12 Aug. There
is no per-rule record of first deployment anywhere, so commit time is not a
compromise — it is the only signal that exists. Do not "improve" this by
reaching for the bundle.

MERGE ORDER IS NOT CREATION ORDER, AND DOES NOT NEED TO BE. If two intents merge
before an apply runs, that single apply creates both and Terraform decides their
relative order, not git. The pilot's three oldest rules had distinct commit times
(00:25, 08:59, 19:10 on 26 Jul) and still sat reversed in SCM. This does not
break the model because apply RE-ASSERTS the order every run: the rulebase
converges on commit order regardless of what the provider did while creating
them. Batched merges self-heal on the next apply.

THE ORDER OF RULES DEPLOYED TOGETHER IS NOT GUARANTEED. "Append at bottom" only
yields a deterministic sequence when rules are created one at a time. Three of
the pilot's rules were created in a single apply on 2026-07-26, before the
`-parallelism=1` guard existed, and sit in SCM reversed relative to the order
their intents landed. Whether that was parallel creation or somebody reordering
them in the console is UNKNOWABLE — nothing was watching, which is the condition
this module exists to end. Ties are therefore broken by name, so the expected
order is total and reproducible even for a batch.

ONLY MANAGED RULES ARE COMPARED. A folder holds rules this platform never
created, and where they sit is not ours to assert. What must hold is that OUR
rules keep their mutual order; a device-local rule between two of them is not
drift.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class OrderHistoryUnavailable(RuntimeError):
    """Git history could not answer when an intent was deployed.

    RAISED, NEVER DEFAULTED. A shallow clone (`actions/checkout` defaults to
    `fetch-depth: 1`) returns no commits, and every intent would then look like
    it was deployed at the same instant — collapsing the expected order to
    alphabetical and reporting confident, wrong drift on rules nobody touched.
    A check that cannot see its input must say so, not guess.
    """


def _no_history(path: Path, detail: str = "") -> str:
    return (
        f"git history has no commit adding {path}"
        + (f" ({detail})" if detail else "")
        + ". A shallow clone cannot answer when a rule was deployed — the job "
          "needs `fetch-depth: 0` on actions/checkout. Refusing to guess: with "
          "no history every rule looks deployed at the same instant, the "
          "expected order collapses to alphabetical, and the check reports "
          "confident drift on rules nobody touched."
    )


def deployed_at(path: Path, *, repo: Optional[Path] = None) -> int:
    """Unix time of the commit that FIRST added `path`.

    `--diff-filter=A`, taking the LAST line: the first addition, not the latest
    touch. Editing a rule does not move it in the rulebase, so the deployment
    moment is the one that matters.

    NO `--follow`. It was there first and made this silently wrong: rename
    detection walks PAST the file's own history into whatever git thinks it was
    renamed from, so every intent resolved to the repository's FIRST commit and
    all timestamps came back identical. The expected order then collapsed to the
    alphabetical tie-break — the exact failure the shallow-clone guard exists to
    prevent, arriving through a different door and without an error. Following
    renames is meaningless here anyway: a rule's name IS its filename, so a
    renamed intent is a different rule.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%ct", "--", str(path)],
            cwd=str(repo) if repo else None,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        # SAME MESSAGE AS THE EMPTY CASE. A repository with no commits exits
        # 128 rather than returning nothing, so routing it to a bare "git
        # failed" would have hidden the one cause an operator can actually fix.
        raise OrderHistoryUnavailable(_no_history(path, str(e))) from e
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise OrderHistoryUnavailable(_no_history(path))
    return int(lines[-1])


def expected_order(intents: Dict[str, Path], *, repo: Optional[Path] = None) -> List[str]:
    """Rule names in the order they were deployed, ties broken by name."""
    stamped: List[Tuple[int, str]] = [
        (deployed_at(path, repo=repo), name) for name, path in intents.items()
    ]
    return [name for _, name in sorted(stamped, key=lambda t: (t[0], t[1]))]


@dataclass(frozen=True)
class OrderReport:
    scope: str
    expected: Sequence[str]
    actual: Sequence[str]

    @property
    def is_clean(self) -> bool:
        return list(self.expected) == list(self.actual)

    @property
    def first_difference(self) -> Optional[int]:
        for i, (e, a) in enumerate(zip(self.expected, self.actual)):
            if e != a:
                return i
        return None if len(self.expected) == len(self.actual) else min(
            len(self.expected), len(self.actual))

    def moved(self) -> List[str]:
        """Rules whose index changed — what a report should name."""
        ai = {n: i for i, n in enumerate(self.actual)}
        ei = {n: i for i, n in enumerate(self.expected)}
        return sorted(n for n in ei if n in ai and ei[n] != ai[n])

    def summary(self) -> str:
        if not self.expected:
            return f"{self.scope}: no managed rules to order"
        if self.is_clean:
            return (f"{self.scope}: {len(self.actual)} managed rule(s) in "
                    f"deployment order")
        return "\n".join([
            f"{self.scope}: RULE ORDER DRIFT — managed rules are not in "
            f"deployment order",
            f"  expected: {' -> '.join(self.expected)}",
            f"  actual:   {' -> '.join(self.actual)}",
            f"  moved:    {', '.join(self.moved()) or '(membership differs)'}",
        ])


def detect_order(*, scope: str, expected: Sequence[str],
                 actual_rulebase: Iterable[str]) -> OrderReport:
    """Compare our rules' mutual order, ignoring everything else in the folder.

    `actual_rulebase` is EVERY rule name in rulebase order. It is filtered to the
    expected set here rather than by the caller, so a rule this platform did not
    create can sit anywhere between ours without being reported.
    """
    want = set(expected)
    actual = [n for n in actual_rulebase if n in want]
    return OrderReport(scope=scope, expected=list(expected), actual=actual)


def moves_to_restore(report: OrderReport) -> List[Tuple[str, str]]:
    """`(rule, move_after_this_rule)` pairs that put the rulebase back.

    ANCHORED TO THE PREVIOUS MANAGED RULE, not to `bottom`. Moving each rule to
    the bottom in turn would also drag the whole managed block beneath any
    unmanaged rule currently sitting below it — changing this platform's
    relationship to config it does not own, in the name of fixing our own
    internal order. Anchoring leaves the first rule exactly where it is and
    corrects only the sequence after it.
    """
    if report.is_clean or not report.expected:
        return []
    return [(name, report.expected[i - 1])
            for i, name in enumerate(report.expected) if i > 0]
