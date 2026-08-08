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


# ── MODIFICATIONS ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Modification:
    """An intent present in both trees whose SPEC changed."""

    kind: str
    req_id: str
    before: Any          # the baseline request
    after: Any           # the current request
    path: Optional[str] = None


def find_modifications(baseline: Dict[Tuple[str, str], "Removal"],
                       current: Dict[Tuple[str, str], "Removal"]) -> List[Modification]:
    """Entries in both trees whose `spec` differs.

    Compares the LOADED spec, not the raw YAML: reformatting, comment edits and
    key reordering are not changes to the firewall, and flagging them would make
    the ticket rule fire on edits that alter nothing.
    """
    out: List[Modification] = []
    for key, cur in sorted(current.items()):
        base = baseline.get(key)
        if base is None:
            continue                                    # an addition
        if getattr(base.request, "spec", None) != getattr(cur.request, "spec", None):
            out.append(Modification(kind=key[0], req_id=key[1],
                                    before=base.request, after=cur.request,
                                    path=cur.path))
    return out


def stale_ticket_problems(mods: Iterable[Modification]) -> List[str]:
    """Modified intents still carrying the ticket that authorised the OLD state.

    WHY THIS IS A REJECTION, NOT A NOTE. `metadata` describes a REQUEST — a
    one-time event — while the file is a RULE, a long-lived object. Editing the
    object does not update the event record, so the evidence bundle for today's
    change names whoever asked for the ORIGINAL one.

    Measured on 2026-08-08: widening `REQ-2026-0727` from a /24 to a /16 produced
    a bundle reading `ticket: JIRA-20727, requested: 2026-07-26, justification:
    "App tier resolves names via the internal DNS resolver"` — a ticket that does
    not cover the change, a date six weeks earlier, and a justification for a
    narrower rule. Only `intent_sha256` moved.

    The bundle claims NIST CM-3 (request -> review -> approve -> implement) and
    named the wrong request. That is a FALSE STATEMENT in a compliance artifact,
    not a missing field, so it fails the change rather than annotating it.

    Only a SPEC change requires a new ticket. Editing a justification or a
    comment alters nothing on the firewall and needs no change record.
    """
    problems: List[str] = []
    for m in mods:
        before_t = getattr(getattr(m.before, "metadata", None), "ticket", None)
        after_t = getattr(getattr(m.after, "metadata", None), "ticket", None)
        if before_t == after_t:
            problems.append(
                f"{m.req_id} ({m.kind}): `spec` changed but `metadata.ticket` is still "
                f"{after_t!r}. A change needs its own change ticket — otherwise the "
                f"evidence bundle for this change names the request that authorised the "
                f"PREVIOUS one. Update `metadata.ticket` (and `requested`, and "
                f"`justification` if the reason differs) to describe THIS change."
            )
    return problems
