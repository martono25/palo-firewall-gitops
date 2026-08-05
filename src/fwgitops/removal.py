"""Classify what a PR REMOVES, not just what it adds.

WHY THIS EXISTS. A deletion was invisible to the entire risk pipeline:

    classify   reads the intent TREE — a deleted intent is simply absent, so
               there is nothing to classify
    risk gate  never sees it; it passes on the remaining set
    evidence   bundles are built per request — no request, no audit record
    terraform  the ONLY stage that knows, and only at plan time

So removing a rule that permits traffic and removing a route that carries it
were both unclassified, unaudited and auto-appliable — not because anyone judged
them low risk, but because nothing classified them at all. That is the same shape
as the `expires` field: a control that looks like it covers everything and
silently does not cover a whole class of change.

WHY TREE-vs-TREE RATHER THAN GIT. A removal is a property of a CHANGE, so it
needs a baseline. Reading git here would make the classifier impure and
untestable without a repository; instead the caller materialises the base
revision's intent tree (CI does `git archive`) and passes it in. Same reasoning
that keeps the compiler off the SCM API.

RISK IS PER KIND, AND IS NOT THE MIRROR OF CREATION. Removing an `allow`
withdraws access — it can break an application, but it opens nothing. Removing a
`deny` does the opposite: traffic that was blocked may now match a broader allow
below it. Those are not the same act and must not share a tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fwgitops.evidence import RiskVerdict

#: Bumped when a removal's tier or reasoning changes, so an evidence bundle can
#: be read against the rules that produced it.
REMOVAL_CLASSIFIER_VERSION = "1.0.0"


@dataclass(frozen=True)
class Removal:
    """An intent present in the baseline and absent now."""

    kind: str
    req_id: str
    #: The loaded request object from the BASELINE tree. A removal is classified
    #: on what it used to be — nothing in the current tree describes it.
    request: Any
    #: Where the file lived, for the report.
    path: Optional[str] = None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind, self.req_id)


def find_removals(baseline: Dict[Tuple[str, str], Removal],
                  current_keys) -> List[Removal]:
    """Baseline entries whose (kind, id) no longer appears.

    Keyed on kind AND id: the same id may not be reused across kinds, but keying
    on id alone would make a kind change look like neither a removal nor an
    addition.
    """
    now = set(current_keys)
    return [r for key, r in sorted(baseline.items()) if key not in now]


def classify_removal(removal: Removal) -> RiskVerdict:
    """Tier a removal, with the reason attached.

    Fail-closed: an unrecognised kind is CRITICAL rather than LOW. A kind added
    later without a removal rule here must be looked at, not waved through — the
    default that silently permits is how a whole class of change goes unassessed,
    which is the bug this module exists to fix.
    """
    fired: List[Dict[str, str]] = []

    def fire(tier: str, check: str, reason: str) -> str:
        fired.append({"check": check, "reason": reason, "tier": tier})
        return tier

    kind = removal.kind
    spec = getattr(removal.request, "spec", None)

    if kind == "AccessRequest":
        action = (getattr(spec, "action", "") or "").lower()
        if action == "allow":
            tier = fire("LOW", "allow_rule_removed",
                        f"removes an allow rule ({removal.req_id}) — withdraws access. It "
                        f"can break whatever depended on it, but it opens nothing.")
        else:
            # The asymmetry that makes a removal its own question: traffic this
            # rule blocked may now fall through to a broader allow beneath it.
            tier = fire("HIGH", "deny_rule_removed",
                        f"removes a {action or 'deny'} rule ({removal.req_id}) — traffic it "
                        f"blocked may now match a permissive rule below it. A removal that "
                        f"INCREASES effective access.")
    elif kind == "RouteRequest":
        tier = fire("HIGH", "route_removed",
                    f"removes a static route ({removal.req_id}). Traffic that used it stops "
                    f"forwarding or falls to a different next hop. Nothing refuses this: "
                    f"unlike a zone, a router with one fewer route is still a valid object.")
    elif kind == "ZoneRequest":
        tier = fire("HIGH", "zone_removed",
                    f"removes a zone ({removal.req_id}). Interfaces bound to it lose their "
                    f"zone; PAN-OS drops traffic on an unzoned interface. SCM refuses the "
                    f"delete while a rule still references it, so the likely outcomes are a "
                    f"failed apply or silently dropped traffic.")
    elif kind == "InterfaceRequest":
        tier = fire("HIGH", "interface_config_removed",
                    f"removes interface configuration ({removal.req_id}). A device-scope "
                    f"override reverts to the inherited object, which carries no addressing "
                    f"— the firewall loses the IP on that interface.")
    else:
        tier = fire("CRITICAL", "unknown_kind_removed",
                    f"removes a {kind!r} ({removal.req_id}), a kind with no removal rule. "
                    f"Tiered CRITICAL deliberately: a default that permits is how a class of "
                    f"change goes unassessed.")

    return RiskVerdict(
        tier=tier,
        classifier_version=REMOVAL_CLASSIFIER_VERSION,
        checks_fired=tuple(fired),
    )
