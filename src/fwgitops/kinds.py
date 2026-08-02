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

    compile · tfvars filename · tfvars payload · folder · classify

NOT uniform, and deliberately not forced into one signature:

  * EVIDENCE — `build_bundle` takes a rule's request AND its compiled change and
    reaches into rule-specific fields. There is no kind-agnostic bundle today.
  * DRIFT — genuinely two engines. Rules carry `gitops:` provenance tags, so
    drift can say WHO created something. `scm_zone` has no `tag` attribute (only
    14 of the provider's resources do), so zones use state-based drift against a
    declared set plus a baseline allowlist. Same word, different mechanism.

A protocol with optional members for the stages a kind cannot support would be
an interface with holes — barely better than the isinstance chains it replaced.
So capability is DECLARED (`drift_engine`, `has_evidence`) rather than faked,
and a caller that needs one asks instead of assuming. Honest beats uniform.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    to_tfvars,
    zone_tfvars,
)
from fwgitops.compiler import _compile_interface as _compile_interface_impl
from fwgitops.compiler import _compile_route as _compile_route_impl
from fwgitops.compiler import _compile_zone as _compile_zone_impl
from fwgitops.intent import AccessRequest, InterfaceRequest, RouteRequest, ZoneRequest


@dataclass(frozen=True)
class KindHandler:
    """Everything the pipeline needs to know about one intent kind."""

    kind: str                                   # matches the intent's `kind:`
    request_type: Type[Any]                     # the loaded intent dataclass
    compiled_type: Type[Any]                    # what `compile` returns
    compile: Callable[[Any, Any], Any]          # (request, env_map) -> compiled
    tfvars_filename: str                        # per-folder output file
    tfvars: Callable[[List[Any]], Dict[str, Any]]   # compiled[] -> tfvars payload
    folder_of: Callable[[Any], str]             # compiled -> SCM folder
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
    #: Whether `evidence.build_bundle` accepts this kind at all.
    has_evidence: bool


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
        tfvars=to_tfvars,
        folder_of=lambda c: c.rule.folder,
        name_of=lambda c: c.rule.name,
        classify=_rule_classify,
        report_prefix="",
        drift_engine="tag",
        state_api_path=None,   # rules use the tag-based engine      # rules carry gitops: provenance tags
        has_evidence=True,
    ),
    "InterfaceRequest": KindHandler(
        kind="InterfaceRequest",
        request_type=InterfaceRequest,
        compiled_type=CompiledInterface,
        compile=_compile_interface_impl,
        tfvars_filename="interfaces.auto.tfvars.json",
        tfvars=interface_tfvars,
        folder_of=lambda c: c.folder,
        name_of=lambda c: c.name,
        classify=_interface_classify,
        report_prefix="interface/",
        drift_engine="state",
        state_api_path="/config/network/v1/ethernet-interfaces",    # scm_ethernet_interface has no `tag` attribute
        has_evidence=False,      # bundles are rule-shaped today
    ),
    "RouteRequest": KindHandler(
        kind="RouteRequest",
        request_type=RouteRequest,
        compiled_type=CompiledRoute,
        compile=_compile_route_impl,
        tfvars_filename="routers.auto.tfvars.json",
        tfvars=route_tfvars,
        folder_of=lambda c: c.folder,
        # Routes AGGREGATE — many share one router object — so the report name
        # is the route's own id, not the router's.
        name_of=lambda c: c.name,
        classify=_route_classify,
        report_prefix="route/",
        drift_engine="state",    # scm_logical_router has no `tag` attribute
        state_api_path="/config/network/v1/logical-routers",
        has_evidence=False,
    ),
    "ZoneRequest": KindHandler(
        kind="ZoneRequest",
        request_type=ZoneRequest,
        compiled_type=CompiledZone,
        compile=_compile_zone_impl,
        tfvars_filename="zones.auto.tfvars.json",
        tfvars=zone_tfvars,
        folder_of=lambda c: c.folder,
        name_of=lambda c: c.name,
        classify=_zone_classify,
        report_prefix="zone/",
        drift_engine="state",
        state_api_path="/config/network/v1/zones",    # scm_zone has no `tag` attribute
        has_evidence=False,      # bundles are rule-shaped today
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


def group_by_kind_and_folder(
    compiled: List[Any],
) -> Dict[Tuple[str, str], List[Any]]:
    """(kind, folder) -> compiled objects. The one grouping the CLI needs."""
    out: Dict[Tuple[str, str], List[Any]] = {}
    for obj in compiled:
        handler = handler_for_compiled(obj)
        out.setdefault((handler.kind, handler.folder_of(obj)), []).append(obj)
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
