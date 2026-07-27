"""Drift detection — declared desired state (Git) vs actual SCM config.

GitOps trusts Git, not the running config. Drift is when the two diverge, and the
design's stance is detect-and-alert. There are three tag-based drift classes,
scoped to a managed folder:

  * UNMANAGED — an actual rule with no `gitops:managed` tag: added directly in
    SCM, outside GitOps. terraform CANNOT see these (they aren't in its state),
    which is exactly why this tag-based check exists.
  * ORPHANED  — a gitops-managed rule live in SCM whose request id is no longer
    in the declared intents: deleted from Git but still applied.
  * MALFORMED — a rule carrying the managed marker but no `gitops:req` tag: a
    broken/hand-edited managed rule (fail-closed — never silently ignored).

(terraform plan already flags managed rules that were MODIFIED/DELETED for
resources IN its state; the scheduled drift workflow uses that. This module
covers the gap: additions terraform can't see, plus orphans, from tags alone.)

The comparison is pure — actual rules are fed in (the SCM read that lists them is
the thin live piece), so the logic is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from fwgitops.compiler import CompiledChange
from fwgitops.tags import is_managed, parse_managed_meta


@dataclass(frozen=True)
class ActualRule:
    """A security rule as it currently exists in SCM (what a folder read returns)."""

    folder: str
    name: str
    tags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DriftReport:
    unmanaged: Tuple[ActualRule, ...] = ()
    orphaned: Tuple[ActualRule, ...] = ()
    malformed: Tuple[ActualRule, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.unmanaged or self.orphaned or self.malformed)

    @property
    def count(self) -> int:
        return len(self.unmanaged) + len(self.orphaned) + len(self.malformed)

    def summary(self) -> str:
        if self.is_clean:
            return "no drift: SCM matches the declared policy"
        parts = []
        for label, rules in (("unmanaged", self.unmanaged), ("orphaned", self.orphaned),
                             ("malformed", self.malformed)):
            for r in rules:
                parts.append(f"  {label:9} {r.folder}/{r.name}")
        return f"DRIFT — {self.count} rule(s):\n" + "\n".join(parts)


def detect_drift(
    desired: Iterable[CompiledChange], actual: Iterable[ActualRule]
) -> DriftReport:
    """Compare the declared managed rules against the folder's actual rules."""
    declared = {(ch.rule.folder, ch.rule.name) for ch in desired}
    unmanaged: List[ActualRule] = []
    orphaned: List[ActualRule] = []
    malformed: List[ActualRule] = []
    for r in actual:
        if not is_managed(r.tags):
            unmanaged.append(r)  # no managed marker -> added outside GitOps
            continue
        try:
            meta = parse_managed_meta(r.tags)
        except ValueError:
            malformed.append(r)  # managed marker but no gitops:req tag
            continue
        if meta is None or (r.folder, meta.req_id) not in declared:
            orphaned.append(r)   # managed, but not in the current declared set
    return DriftReport(tuple(unmanaged), tuple(orphaned), tuple(malformed))
