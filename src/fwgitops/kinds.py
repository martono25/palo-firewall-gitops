"""The intent-kind registry (ADR-0001's promise, finally kept).

ADR-0001 said adding an object type means plugging into a registry. In practice
only the intent LOADER was registered; everything downstream branched on Python
type — `compile_any` on isinstance, the CLI filtering compiled objects by class
eleven separate times, two classify entry points, two drift engines. Adding a
kind meant touching about eight places and remembering all of them.

That is not a hypothetical cost. `ZoneRequest` was wired into the three stages
someone remembered and silently omitted from the four nobody did, and because
Terraform ignores an undeclared auto-tfvars variable with exit 0, it shipped for
a whole release without ever reaching a firewall (ADR-0004).

WHAT THIS REGISTRY DOES AND DOES NOT UNIFY
------------------------------------------
Uniform across kinds, so registered here as callables:

    compile · tfvars filename · tfvars payload · folder · classify · evidence

EVIDENCE WAS NOT UNIFORM, AND THAT WAS A HOLE, NOT A DESIGN. Until v1.36.0 this
docstring argued that `build_bundle` reaching into rule-specific fields made a
kind-agnostic bundle impossible. The consequence was measured on 2026-08-08: ten
intents produced FIVE bundles. A `RouteRequest` decides where every unmatched
packet goes, and changing one left no audit record at all — while the pipeline
reported success and the workflow committed the bundles it did have. The
"capability is declared, not faked" principle is right, but it was being used to
declare a gap permanent instead of describing one.

What made the bundle rule-shaped was an EXPLICIT field list in `evidence.py`.
The fix is `evidence_object` below: the compiled dataclass serialised whole, so
a kind describes itself and a field added to a compiled type appears in the
bundle without anyone remembering to add it.

STILL not uniform, and deliberately not forced into one signature:

  * DRIFT — genuinely two engines. Rules carry `gitops:` provenance tags, so
    drift can say WHO created something. `scm_zone` has no `tag` attribute (only
    14 of the provider's resources do), so zones use state-based drift against a
    declared set plus a baseline allowlist. Same word, different mechanism.

A protocol with optional members for the stages a kind cannot support would be
an interface with holes — barely better than the isinstance chains it replaced.
So capability is DECLARED (`drift_engine`) rather than faked, and a caller that
needs one asks instead of assuming. Honest beats uniform — but "honest" has to
be re-earned each time, because a declared gap is still a gap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from fwgitops.compiler import (
    CompileError,
    CompiledChange,
    CompiledInterface,
    CompiledRoute,
    CompiledZone,
    compile_request,
    interface_tfvars,
    route_tfvars,
    scope_of as scope_of_compiled,
    to_tfvars,
    to_tfvars_written,
    zone_tfvars,
)
from fwgitops.compiler import _compile_interface as _compile_interface_impl
from fwgitops.compiler import _compile_route as _compile_route_impl
from fwgitops.compiler import _compile_zone as _compile_zone_impl
from fwgitops.intent import AccessRequest, InterfaceRequest, RouteRequest, ZoneRequest


#: Scope lives ONCE in a bundle, as `compiled.scope`. Repeating it inside the
#: object would let the two disagree, and a bundle that disagrees with itself
#: about which firewall a change landed on is worse than one that omits it.
_SCOPE_FIELDS = ("folder", "device")


def _default_evidence_object(compiled: Any) -> Dict[str, Any]:
    """The compiled dataclass, serialised whole, minus its scope fields.

    Serialised WHOLE on purpose. The v1 bundle listed the rule's fields by hand
    and the list went stale twice — `application`, `profile_group` and
    `log_setting` were on the compiled rule for a release before anyone added
    them here, so bundles claiming to be "the effective rule an assessor sees"
    omitted the threat-inspection profile. An audit record that has to be
    remembered separately from the thing it records will eventually not be.
    """
    if not is_dataclass(compiled):
        raise TypeError(
            f"{type(compiled).__name__} is not a dataclass, so it has no default "
            f"evidence shape — register an `evidence_object` for its kind")
    return {k: v for k, v in asdict(compiled).items() if k not in _SCOPE_FIELDS}


def _no_evidence_id(compiled: Any) -> Optional[str]:
    """This kind's compiled object does not carry its request id. See the field."""
    return None


@dataclass(frozen=True)
class KindHandler:
    """Everything the pipeline needs to know about one intent kind."""

    kind: str                                   # matches the intent's `kind:`
    request_type: Type[Any]                     # the loaded intent dataclass
    compiled_type: Type[Any]                    # what `compile` returns
    compile: Callable[[Any, Any], Any]          # (request, env_map) -> compiled
    tfvars_filename: str                        # per-folder output file
    tfvars: Callable[[List[Any]], Dict[str, Any]]   # compiled[] -> tfvars payload
    #: compiled -> Scope (an SCM folder, or a single firewall). Not a bare
    #: folder string: a firewall is the last level of the SCM hierarchy but is
    #: addressed `device=`, never `folder=`, so the scope carries which.
    scope_of: Callable[[Any], Any]
    name_of: Callable[[Any], str]               # compiled -> human name (reports)
    classify: Callable[..., Any]                # compiled -> RiskVerdict
    #: Prefix for report lines, so `dmz` (a zone) is not mistaken for a rule.
    #: Display metadata, legitimately per-kind — not a behaviour switch.
    report_prefix: str
    #: "tag" (provenance tags exist) or "state" (they do not). NOT a shared
    #: signature — see the module docstring.
    drift_engine: str
    #: SCM read path for this kind's CURRENT state, or None where the kind does
    #: not use state-based comparison. Registered here so the snapshot command
    #: and the state-aware classifier checks are driven off the registry rather
    #: than a hand-written block per kind.
    state_api_path: Optional[str]
    #: compiled -> the `compiled.object` block of an evidence bundle. Defaults to
    #: the whole compiled dataclass minus its scope fields (which the bundle
    #: records once, as `compiled.scope`), so a field added to a compiled type
    #: reaches the audit record without a second edit here. A kind overrides this
    #: only to say something the dataclass does not.
    #:
    #: `default_factory`, not a plain default: a bare function assigned as a
    #: dataclass default becomes a CLASS attribute, and Python then binds it as a
    #: method — `handler.evidence_object(c)` would silently pass the handler as
    #: the first argument. The factory stores it per instance instead.
    evidence_object: Callable[[Any], Dict[str, Any]] = field(
        default_factory=lambda: _default_evidence_object)
    #: compiled -> the request id the object carries, or None where it carries
    #: none. A rule and a route are NAMED for their request, so a bundle can
    #: verify it is pairing the right intent with the right object; a zone is
    #: named `dmz`, so there is nothing to check and the guard says so rather
    #: than inventing an assertion it cannot make.
    evidence_id_of: Callable[[Any], Optional[str]] = field(
        default_factory=lambda: _no_evidence_id)
    #: Kinds that must be APPLIED BEFORE this one (ADR-0002's ordered chain).
    #: Declared per kind rather than hard-coded in the CLI, for the same reason
    #: `drift_engine` is: a new kind states its own requirements and the
    #: sequencing follows, instead of a list somewhere else needing an edit.
    #:
    #: This is NOT a substitute for Terraform's graph. Within one root Terraform
    #: orders by resource reference and does it better. This exists because the
    #: chain SPANS ROOTS — interfaces are device-scoped, zones/routes/rules are
    #: folder-scoped, so they live in separate states that no single graph
    #: covers.
    depends_on_kinds: Tuple[str, ...] = ()


def _rule_classify(compiled: CompiledChange, **ctx: Any) -> Any:
    from fwgitops.classify import classify

    return classify(compiled, policy=ctx.get("policy"), hierarchy=ctx.get("hierarchy"))


def _interface_classify(compiled: CompiledInterface, **ctx: Any) -> Any:
    from fwgitops.classify import classify_interface

    return classify_interface(compiled, hierarchy=ctx.get("hierarchy"),
                              current=ctx.get("current"))


def _route_classify(compiled: CompiledRoute, **ctx: Any) -> Any:
    from fwgitops.classify import classify_route

    return classify_route(compiled, hierarchy=ctx.get("hierarchy"),
                          current=ctx.get("current"))


def _zone_classify(compiled: CompiledZone, **ctx: Any) -> Any:
    from fwgitops.classify import classify_zone

    return classify_zone(compiled, hierarchy=ctx.get("hierarchy"),
                         current=ctx.get("current"))


#: kind name -> handler. Adding a kind means ONE entry here plus its Terraform
#: resource; the compile→tfvars→contract path then covers it automatically, and
#: `fwgitops compile` fails closed if the Terraform side is missing (ADR-0004).
REGISTRY: Dict[str, KindHandler] = {
    "AccessRequest": KindHandler(
        kind="AccessRequest",
        request_type=AccessRequest,
        compiled_type=CompiledChange,
        compile=lambda req, env_map: compile_request(req, env_map),
        tfvars_filename="rules.auto.tfvars.json",
        tfvars=to_tfvars_written,
        scope_of=lambda c: scope_of_compiled(c.rule),
        name_of=lambda c: c.rule.name,
        classify=_rule_classify,
        # Rules reference zones. Within a folder Terraform already enforces this
        # via scm_zone.this[z].name; declared here so the CROSS-ROOT case (rules
        # in a folder, interfaces on a device) is ordered too.
        depends_on_kinds=("InterfaceRequest", "ZoneRequest", "RouteRequest"),
        report_prefix="",
        drift_engine="tag",
        state_api_path=None,   # rules use the tag-based engine      # rules carry gitops: provenance tags
        # A CompiledChange is a rule PLUS the address/service objects it needs,
        # so the default (serialise the dataclass whole) already yields all
        # three. The rule is lifted to the top so `object.rule` reads the same
        # as it did in the v1 schema.
        evidence_object=lambda c: {
            "rule": _default_evidence_object(c.rule),
            "address_objects": [_default_evidence_object(o) for o in c.address_objects],
            "service_objects": [_default_evidence_object(o) for o in c.service_objects],
        },
        evidence_id_of=lambda c: c.rule.name,
    ),
    "InterfaceRequest": KindHandler(
        kind="InterfaceRequest",
        request_type=InterfaceRequest,
        compiled_type=CompiledInterface,
        compile=_compile_interface_impl,
        tfvars_filename="interfaces.auto.tfvars.json",
        tfvars=interface_tfvars,
        scope_of=scope_of_compiled,
        name_of=lambda c: c.name,
        classify=_interface_classify,
        depends_on_kinds=(),                      # first link in the chain
        report_prefix="interface/",
        drift_engine="state",
        state_api_path="/config/network/v1/ethernet-interfaces",    # scm_ethernet_interface has no `tag` attribute
    ),
    "RouteRequest": KindHandler(
        kind="RouteRequest",
        request_type=RouteRequest,
        compiled_type=CompiledRoute,
        compile=_compile_route_impl,
        tfvars_filename="routers.auto.tfvars.json",
        tfvars=route_tfvars,
        scope_of=scope_of_compiled,
        # Routes AGGREGATE — many share one router object — so the report name
        # is the route's own id, not the router's.
        name_of=lambda c: c.name,
        classify=_route_classify,
        # A route's VRF owns interfaces, and its next-hop is only reachable once
        # they are addressed. Zones do not gate routing, but ADR-0002 sequences
        # them earlier and keeping the declared chain linear is worth more than
        # the parallelism lost.
        depends_on_kinds=("InterfaceRequest", "ZoneRequest"),
        report_prefix="route/",
        drift_engine="state",    # scm_logical_router has no `tag` attribute
        state_api_path="/config/network/v1/logical-routers",
        # `name` IS the request id — see CompiledRoute. Routes AGGREGATE into one
        # router object, so this is the only kind where the bundle documents a
        # change that shares its Terraform resource with other requests; the
        # tfvars hash it records therefore covers the whole router, not just
        # this route. Recorded rather than smoothed over.
        evidence_id_of=lambda c: c.name,
    ),
    "ZoneRequest": KindHandler(
        kind="ZoneRequest",
        request_type=ZoneRequest,
        compiled_type=CompiledZone,
        compile=_compile_zone_impl,
        tfvars_filename="zones.auto.tfvars.json",
        tfvars=zone_tfvars,
        scope_of=scope_of_compiled,
        name_of=lambda c: c.name,
        classify=_zone_classify,
        depends_on_kinds=("InterfaceRequest",),   # a zone binds interfaces
        report_prefix="zone/",
        drift_engine="state",
        state_api_path="/config/network/v1/zones",    # scm_zone has no `tag` attribute
    ),
}


def handler_for_request(request: Any) -> KindHandler:
    """Handler for a loaded intent. Fail-closed on an unregistered type."""
    for handler in REGISTRY.values():
        if isinstance(request, handler.request_type):
            return handler
    raise CompileError(
        f"no kind registered for request type {type(request).__name__} — "
        f"register it in fwgitops.kinds.REGISTRY"
    )


def handler_for_compiled(compiled: Any) -> KindHandler:
    """Handler for a compiled object. Fail-closed on an unregistered type."""
    for handler in REGISTRY.values():
        if isinstance(compiled, handler.compiled_type):
            return handler
    raise CompileError(
        f"no kind registered for compiled type {type(compiled).__name__} — "
        f"register it in fwgitops.kinds.REGISTRY"
    )


def compile_any(request: Any, env_map: Any, section: Any = None) -> Any:
    """Dispatch a loaded intent to its kind's compiler (ADR-0001)."""
    handler = handler_for_request(request)
    if handler.kind == "AccessRequest" and section is not None:
        return compile_request(request, env_map, section)
    return handler.compile(request, env_map)


class KindOrderError(Exception):
    """The declared kind dependencies do not form a usable order."""


def kind_apply_order(registry: Optional[Dict[str, "KindHandler"]] = None) -> List[str]:
    """Kinds in the order they must be APPLIED — ADR-0002's ordered chain.

    Topological sort over each handler's `depends_on_kinds`, tie-broken
    alphabetically so the order is DETERMINISTIC. Two runs of the same registry
    must produce the same sequence, or a Day-1 build is not reproducible and the
    ordering is decoration.

    Fails closed on a cycle and on a dependency naming an unregistered kind. A
    silently-dropped edge would order things wrongly while looking like it
    worked, which is the failure this whole mechanism exists to prevent.

    NOT a replacement for Terraform's graph. Inside one root Terraform orders by
    resource reference and does it better. This covers what no single graph can:
    the chain spans ROOTS, because interfaces are device-scoped while zones,
    routes and rules are folder-scoped, so they live in separate states.
    """
    reg = REGISTRY if registry is None else registry

    unknown = {
        (kind, dep)
        for kind, h in reg.items()
        for dep in h.depends_on_kinds
        if dep not in reg
    }
    if unknown:
        detail = ", ".join(f"{k} -> {d}" for k, d in sorted(unknown))
        raise KindOrderError(
            f"kind dependency names an unregistered kind: {detail}. Register it, or "
            f"the chain silently omits a step it claims to sequence."
        )

    pending = {k: set(h.depends_on_kinds) for k, h in reg.items()}
    out: List[str] = []
    while pending:
        ready = sorted(k for k, deps in pending.items() if not deps)
        if not ready:
            stuck = ", ".join(sorted(pending))
            raise KindOrderError(
                f"cycle in kind dependencies among: {stuck}. No apply order exists."
            )
        for k in ready:
            out.append(k)
            del pending[k]
        for deps in pending.values():
            deps.difference_update(ready)
    return out


def scopes_in_apply_order(compiled: List[Any]) -> List[Tuple[str, Any]]:
    """(kind, Scope) pairs ordered so dependencies apply first.

    What the CLI and the apply pipeline iterate. Kinds with no ordering
    relationship keep a stable alphabetical scope order, so a diff of two runs
    shows real changes rather than reshuffling.
    """
    order = {k: i for i, k in enumerate(kind_apply_order())}
    groups = group_by_kind_and_scope(compiled)
    return sorted(groups, key=lambda ks: (order[ks[0]], ks[1].key))


def group_by_kind_and_scope(
    compiled: List[Any],
) -> Dict[Tuple[str, Any], List[Any]]:
    """(kind, Scope) -> compiled objects. The one grouping the CLI needs.

    Keyed by Scope, not folder: a firewall-scoped change and its folder's are
    different objects in SCM (a device write creates a per-device override), so
    they must not be merged into one group or one Terraform state.
    """
    out: Dict[Tuple[str, Any], List[Any]] = {}
    for obj in compiled:
        handler = handler_for_compiled(obj)
        out.setdefault((handler.kind, handler.scope_of(obj)), []).append(obj)
    return out


def of_kind(compiled: List[Any], kind: str) -> List[Any]:
    """Every compiled object of one kind. Replaces the CLI's isinstance filters."""
    handler = REGISTRY[kind]
    return [c for c in compiled if isinstance(c, handler.compiled_type)]


def kinds_with_state_api() -> List[KindHandler]:
    """Kinds whose current state can be snapshotted from SCM."""
    return [h for h in REGISTRY.values() if h.state_api_path]


def kinds_with_drift_engine(engine: str) -> List[KindHandler]:
    return [h for h in REGISTRY.values() if h.drift_engine == engine]


def registered_tfvars_filenames() -> Dict[str, str]:
    """kind -> output filename. Used to assert the gitignore/CI cover them all."""
    return {h.kind: h.tfvars_filename for h in REGISTRY.values()}
