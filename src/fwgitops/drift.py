"""Drift detection — declared desired state (Git) vs actual SCM config.

GitOps trusts Git, not the running config. Drift is when the two diverge, and the
design's stance is detect-and-alert.

TWO engines, because one is not enough:

  * TAG-BASED (below) — for objects that carry `gitops:` provenance tags. Can
    tell WHO created something, so it distinguishes orphaned from unmanaged.
    Applies to security rules.
  * STATE-BASED (further down) — for objects that CANNOT carry tags. `scm_zone`
    and `scm_ethernet_interface` have no `tag` attribute, and only 14 of the
    provider's resources do, so two of the three intent kinds on the roadmap are
    invisible to the tag-based checks. State-based drift compares against the
    declared set plus a baseline allowlist instead of reading provenance.

The tag-based classes, scoped to a managed folder:

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

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fwgitops.compiler import CompiledChange, CompiledZone
from fwgitops.tags import is_managed, parse_managed_meta


@dataclass(frozen=True)
class ActualRule:
    """A security rule as it currently exists in SCM (what a folder read returns)."""

    #: The folder the rule is DEFINED in. For an inherited rule this is an
    #: ANCESTOR of the folder that was queried — same distinction ActualObject
    #: makes for the state engine.
    folder: str
    name: str
    tags: Tuple[str, ...] = ()
    #: The folder that was QUERIED. Distinct from `folder` whenever the rule is
    #: inherited; None for a snapshot that does not record it.
    scope: Optional[str] = None

    @property
    def is_inherited(self) -> bool:
        """Defined in an ancestor folder, not the one under inspection."""
        return self.scope is not None and self.scope != self.folder


@dataclass(frozen=True)
class DriftReport:
    unmanaged: Tuple[ActualRule, ...] = ()
    orphaned: Tuple[ActualRule, ...] = ()
    malformed: Tuple[ActualRule, ...] = ()
    #: Rules owned by an ancestor folder. REPORTED, never counted as drift —
    #: see detect_drift.
    inherited: Tuple[ActualRule, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.unmanaged or self.orphaned or self.malformed)

    @property
    def count(self) -> int:
        return len(self.unmanaged) + len(self.orphaned) + len(self.malformed)

    def summary(self) -> str:
        # Reported either way: "we looked and skipped N" is a different claim
        # from "there was nothing", and only one of them is checkable.
        note = ""
        if self.inherited:
            folders = sorted({r.folder for r in self.inherited})
            note = (f"\n  ({len(self.inherited)} inherited rule(s) from {folders} "
                    f"not checked — owned by an ancestor folder)")
        if self.is_clean:
            return "no drift: SCM matches the declared policy" + note
        parts = []
        for label, rules in (("unmanaged", self.unmanaged), ("orphaned", self.orphaned),
                             ("malformed", self.malformed)):
            for r in rules:
                parts.append(f"  {label:9} {r.folder}/{r.name}")
        return f"DRIFT — {self.count} rule(s):\n" + "\n".join(parts) + note


def detect_drift(
    desired: Iterable[CompiledChange], actual: Iterable[ActualRule]
) -> DriftReport:
    """Compare the declared managed rules against the folder's actual rules.

    INHERITED RULES ARE NOT DRIFT. A folder read returns everything that applies
    to it, including rules defined in ancestors — PAN-OS defaults like
    `All/default` and snippet-provided rules like
    `ngfw-shared/Auto-VPN-Default-Snippet`. None carries a `gitops:` tag, so
    they all look "added outside GitOps", and on the first live run they were
    all reported as drift: six of them, permanently, in a job whose warning is
    the alert.

    They are not ours and not the queried folder's; whoever owns that ancestor
    owns them. The state engine already drew this line, and drawing it
    differently here would mean the same rule is drift or not depending on which
    engine looked at it.
    """
    declared = {(ch.rule.folder, ch.rule.name) for ch in desired}
    unmanaged: List[ActualRule] = []
    orphaned: List[ActualRule] = []
    malformed: List[ActualRule] = []
    inherited: List[ActualRule] = []
    for r in actual:
        if r.is_inherited:
            inherited.append(r)
            continue
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
    return DriftReport(tuple(unmanaged), tuple(orphaned), tuple(malformed),
                       tuple(inherited))


# ── State-based drift, for objects that CANNOT carry tags ──────────────────
#
# Everything above keys off `gitops:` tags. That works for security rules and
# fails for most object types: `scm_zone` and `scm_ethernet_interface` have no
# `tag` attribute at all, and only 14 of the provider's resources do. Two of the
# three intent kinds on the roadmap are therefore invisible to the checks above.
#
# Without a provenance marker you cannot ask "did WE create this?". You can still
# ask the two questions that matter, and this is what state-based drift does:
#
#   * Is something here that Git never declared and the platform never listed as
#     pre-existing?  -> UNEXPECTED
#   * Is something Git declares missing from SCM?  -> MISSING
#   * Does a declared object's live config differ from what Git says? -> MODIFIED
#
# The `baseline_zones` allowlist (env map) is what makes UNEXPECTED meaningful:
# it names the objects that legitimately pre-date GitOps, so anything outside
# (declared ∪ baseline) is genuinely unaccounted for.
#
# HONEST LIMIT, and it is not fixable without tags: UNEXPECTED cannot distinguish
# "we created it and the intent was later deleted" (an orphan) from "someone
# created it by hand" (unmanaged). The tag-based checks above CAN, because a rule
# carries its own provenance. Here there is nothing to read, so both collapse
# into one class. Do not report it as though the cause is known.


@dataclass(frozen=True)
class ActualObject:
    """An untaggable SCM object as it currently exists (zone, interface, …).

    `fields` is the provider-shaped view of the object, exactly as the SCM read
    returns it, so it can be compared against the compiler's tfvars output
    without a translation layer in between.
    """

    kind: str                     # "zone", "interface", …
    #: The folder the object is DEFINED in, as SCM returns it. For an inherited
    #: object this is an ANCESTOR of the folder that was queried.
    folder: str
    name: str
    fields: Dict[str, Any] = field(default_factory=dict)
    #: The folder that was QUERIED. Distinct from `folder` whenever the object
    #: is inherited. Defaults to `folder` for a locally-defined object.
    scope: Optional[str] = None

    @property
    def scope_folder(self) -> str:
        return self.scope or self.folder

    @property
    def is_inherited(self) -> bool:
        """Defined in an ancestor folder, not the one under inspection."""
        return self.scope is not None and self.scope != self.folder


@dataclass(frozen=True)
class FieldDiff:
    obj: ActualObject
    field_name: str
    declared: Any
    actual: Any


@dataclass(frozen=True)
class ObjectDriftReport:
    unexpected: Tuple[ActualObject, ...] = ()
    missing: Tuple[Tuple[str, str, str], ...] = ()   # (kind, folder, name)
    modified: Tuple[FieldDiff, ...] = ()
    #: Objects defined in an ANCESTOR folder. Not drift — platform config this
    #: folder inherits and does not own. Counted so the summary can say so
    #: rather than silently dropping them.
    inherited: Tuple[ActualObject, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.unexpected or self.missing or self.modified)

    @property
    def count(self) -> int:
        return len(self.unexpected) + len(self.missing) + len(self.modified)

    def summary(self) -> str:
        note = ""
        if self.inherited:
            folders = sorted({o.folder for o in self.inherited})
            note = (f"\n  ({len(self.inherited)} inherited object(s) from {folders} "
                    f"not checked — owned by an ancestor folder)")
        if self.is_clean:
            return "no drift: SCM matches the declared objects" + note
        parts: List[str] = []
        for o in self.unexpected:
            parts.append(f"  unexpected  {o.kind} {o.folder}/{o.name} "
                         f"— present in SCM, neither declared nor a known baseline object")
        for kind, folder, name in self.missing:
            parts.append(f"  missing     {kind} {folder}/{name} — declared in Git, absent from SCM")
        for d in self.modified:
            parts.append(f"  modified    {d.obj.kind} {d.obj.folder}/{d.obj.name}.{d.field_name} "
                         f"— declared {d.declared!r}, actual {d.actual!r}")
        return f"DRIFT — {self.count} finding(s):\n" + "\n".join(parts) + note


def _flatten(prefix: str, value: Any) -> Dict[str, Any]:
    """Flatten one level of nesting so `network.log_setting` is comparable."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            out[f"{prefix}.{k}" if prefix else k] = v
        return out
    return {prefix: value}


def _without_nulls(value: Any) -> Any:
    """Drop keys whose value is None, at any depth.

    An omitted optional and an explicit null are the same statement to this API:
    "not asserted". Normalising both sides keeps that contract consistent inside
    nested structures, where `_flatten` cannot reach.
    """
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def detect_object_drift(
    declared: Dict[Tuple[str, str], Dict[str, Any]],
    actual: Iterable[ActualObject],
    *,
    baseline: Optional[Dict[str, Set[str]]] = None,
) -> ObjectDriftReport:
    """Compare declared untaggable objects against SCM's actual ones.

    `declared` maps (folder, name) -> the provider-shaped desired fields (i.e.
    what the compiler emits for that object). `baseline` maps folder -> names
    that legitimately pre-date GitOps and must NOT be reported as unexpected.

    Only fields the declaration actually SETS are compared. A `None` in the
    declaration means "we did not ask for this", so SCM's value for it is not
    drift — the alternative would flag every provider default as a difference.
    """
    baseline = baseline or {}
    seen: Set[Tuple[str, str]] = set()
    unexpected: List[ActualObject] = []
    modified: List[FieldDiff] = []
    inherited: List[ActualObject] = []

    for obj in actual:
        # An object defined in an ANCESTOR folder is inherited platform config.
        # This folder does not own it, so it is not this folder's drift.
        # Reporting it as "unexpected" produced 7 false positives against the
        # live tenant, where every zone is defined in the shared parent.
        if obj.is_inherited:
            inherited.append(obj)
            continue
        key = (obj.scope_folder, obj.name)
        want = declared.get(key)
        if want is None:
            if obj.name not in baseline.get(obj.scope_folder, set()):
                unexpected.append(obj)
            continue
        seen.add(key)

        flat_want: Dict[str, Any] = {}
        for k, v in want.items():
            flat_want.update(_flatten(k, v) if isinstance(v, dict) else {k: v})
        flat_have: Dict[str, Any] = {}
        for k, v in obj.fields.items():
            flat_have.update(_flatten(k, v) if isinstance(v, dict) else {k: v})

        for fname, declared_value in sorted(flat_want.items()):
            if declared_value is None:
                continue  # not asserted by the declaration
            # THE SAME RULE, ONE LEVEL DOWN. `_flatten` does not descend into
            # lists, so a value like a router's `vrf` is compared whole — and the
            # compiled form carries explicit nulls inside it (`interface: None`,
            # `admin_dist: None`) where SCM simply omits the key. Without this,
            # an untouched router reports as `modified` on every run.
            #
            # "A None in the declaration means we did not ask for this" is
            # already the contract for top-level fields; nested nulls are the
            # same statement about a nested field.
            if _without_nulls(flat_have.get(fname)) != _without_nulls(declared_value):
                modified.append(FieldDiff(obj=obj, field_name=fname,
                                          declared=declared_value,
                                          actual=flat_have.get(fname)))

    inherited_names = {(o.scope_folder, o.name) for o in inherited}
    missing = tuple(
        ("object", folder, name)
        for (folder, name) in sorted(declared)
        if (folder, name) not in seen and (folder, name) not in inherited_names
    )
    return ObjectDriftReport(tuple(unexpected), missing, tuple(modified), tuple(inherited))


def declared_state(handler: Any, objs: Iterable[Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(folder, name) -> desired provider-shaped fields, for `detect_object_drift`.

    Driven off the kind's registered `tfvars` emitter rather than a per-kind
    function, so drift covers a kind the moment it registers `drift_engine`
    "state" — and so the comparison and the thing Terraform applies can never
    disagree about what an object is supposed to look like.

    Was `declared_zone_state`, which meant `InterfaceRequest` declared state-based
    drift in the registry while nothing wired it: the registry made a claim the
    code did not keep.
    """
    # GROUPED BY SCOPE, then ONE tfvars call per group. Two reasons, and the
    # second was a live bug:
    #
    #   * SOME KINDS AGGREGATE. A RouteRequest is not an SCM object — routes
    #     collapse into a logical router, so `tfvars([one_route])` returns a
    #     router keyed by the ROUTER name, not by the request id. Indexing by
    #     `name_of(obj)` raised KeyError: 'REQ-2026-0803'.
    #   * Calling tfvars per object would also compare a router holding ONE
    #     route against SCM's router holding all of them, which is drift that
    #     is not there.
    #
    # Keying on what tfvars PRODUCES is also the more honest comparison: state
    # drift compares SCM objects, and the SCM object is the router, not the
    # request that contributed a route to it.
    by_scope: Dict[str, List[Any]] = {}
    for obj in objs:
        by_scope.setdefault(handler.scope_of(obj).key, []).append(obj)

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for scope, group in by_scope.items():
        payload = handler.tfvars(group)
        # tfvars is {"<variable>": {object_name: fields}}
        inner = next(iter(payload.values()))
        for name, fields in inner.items():
            out[(scope, name)] = fields
    return out
