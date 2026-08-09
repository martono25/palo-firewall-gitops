"""NIST-mapped evidence bundles — one record per change, Git-resident.

The audit artifact that justifies the whole platform: an assessor or incident
responder can reconstruct **what changed, who authorised it, why, and what the
system checked** from the bundle alone — no CI logs, no ticket archaeology, no
SCM UI.

    intent + compiled change + risk verdict + approval + apply + push
                              │
                              ▼
              evidence/<scope>/<REQ-id>.json   (committed; Git = SSoT)

EVERY KIND, NOT JUST RULES (v1.36.0, schema v2). The v1 bundle was rule-shaped:
an explicit list of `SecurityRule` fields, a `request` block reading
`spec.action`, and a path built from `change.rule.folder`. Nothing else could be
expressed, so `fwgitops evidence` filtered to `AccessRequest` and ten intents
produced five bundles — while printing "wrote 5 evidence bundle(s)" and exiting
0. Changing a default route, an interface address or a zone left NO audit
record, which is precisely the class of change an incident responder reaches for
first.

The bundle is now assembled from the kind registry (`kinds.evidence_object`),
so the shape is the compiled dataclass rather than a list someone maintains.
Two consequences worth stating plainly:

  * `request` carries METADATA ONLY. `environment` and `action` moved into
    `compiled.object`, where they belong: metadata is paperwork, spec is
    firewall behaviour, and mixing them is what let a modified rule keep the
    ticket that authorised the previous one (see `removal.stale_ticket_problems`).
  * The path is keyed on SCOPE, not folder. `evidence/device-<serial>/…` for a
    change targeting one firewall, matching the Terraform root layout.

Design properties:
  * **Self-contained** — readable without any other system.
  * **Hashes, not copies** — intent/tfvars/plan referenced by sha256, so bundles
    stay small but tamper-evident. Git supplies history and timestamps.
  * **Versioned** — compiler/classifier/threshold versions are recorded, so a
    past decision stays reproducible after the rules change.
  * **Failures are evidence too** — a rejected or failed change is often more
    audit-relevant than a successful one (a fail-closed push that refused to
    commit unexpected drift is exactly what you want on record).
  * **Deterministic** — byte-stable JSON, so re-generating never churns Git.

Control coverage (NIST SP 800-53 Rev.5): AC-4 (the rule is the flow control),
CM-3 (request → review → approve → implement), CM-5 (who may approve vs who
did), AU-2 / AU-12 (this record IS the audit record), SC-7 (enforced boundary).
AC-5 is added for dual-control (CRITICAL tier, Phase 2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from fwgitops import __version__
from fwgitops.push import PushResult

EVIDENCE_SCHEMA = "fw-evidence/v2"

#: Baseline controls every change record supports.
BASE_CONTROLS: Tuple[str, ...] = ("AC-4", "CM-3", "CM-5", "AU-2", "AU-12", "SC-7")
#: Added when the change went through dual-control approval (CRITICAL tier).
DUAL_CONTROL = "AC-5"

STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_APPLIED, STATUS_REJECTED, STATUS_FAILED)


class EvidenceError(Exception):
    """The bundle could not be built (missing or inconsistent inputs)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


@dataclass(frozen=True)
class CIContext:
    """Facts only the CI run knows. Passed in, never discovered."""

    pr_url: Optional[str] = None
    merge_commit: Optional[str] = None
    run_url: Optional[str] = None
    gate: Optional[str] = None
    approvers: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Dict[str, str], **overrides: Any) -> "CIContext":
        """Build from GitHub Actions env, with explicit overrides winning."""
        server = env.get("GITHUB_SERVER_URL")
        repo = env.get("GITHUB_REPOSITORY")
        run_id = env.get("GITHUB_RUN_ID")
        run_url = (
            f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None
        )
        base = dict(
            pr_url=None,
            merge_commit=env.get("GITHUB_SHA") or None,
            run_url=run_url,
            gate=env.get("GITHUB_ENVIRONMENT") or None,
            approvers=(),
        )
        base.update({k: v for k, v in overrides.items() if v is not None})
        approvers = base.get("approvers") or ()
        base["approvers"] = tuple(approvers)
        return cls(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RiskVerdict:
    """Classifier output. Phase 1 has no classifier, hence the default."""

    tier: str = "not_classified"
    classifier_version: Optional[str] = None
    thresholds_version: Optional[str] = None
    checks_fired: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_dual_control(self) -> bool:
        return self.tier.upper() == "CRITICAL"


def build_bundle(
    *,
    request: Any,
    compiled: Any,
    status: str,
    generated_at: datetime,
    handler: Any = None,
    intent_sha256: Optional[str] = None,
    intent_path: Optional[str] = None,
    tfvars_sha256: Optional[str] = None,
    plan_sha256: Optional[str] = None,
    risk: RiskVerdict = RiskVerdict(),
    ci: CIContext = CIContext(),
    push: Optional[PushResult] = None,
    failure_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the evidence record for one change, whatever kind it is.

    `handler` is the kind registry entry; it is looked up from `request` when
    omitted, so a caller that already has it does not pay for a second search.

    `status` is applied / rejected / failed — failures are recorded, not dropped.
    """
    from fwgitops.kinds import handler_for_request

    if status not in VALID_STATUSES:
        raise EvidenceError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if status in (STATUS_REJECTED, STATUS_FAILED) and not failure_reason:
        raise EvidenceError(f"status {status!r} requires a failure_reason")

    if handler is None:
        handler = handler_for_request(request)
    if not isinstance(compiled, handler.compiled_type):
        raise EvidenceError(
            f"{handler.kind} compiles to {handler.compiled_type.__name__}, got "
            f"{type(compiled).__name__} — refusing to emit mismatched evidence")

    # Where the compiled object carries its request id, the pairing is checked.
    # Where it does not (a zone is named `dmz`), it is NOT — and saying so here
    # is the point: an unchecked pairing that looks checked is how a bundle ends
    # up describing the wrong change while claiming CM-3.
    compiled_id = handler.evidence_id_of(compiled)
    if compiled_id is not None and compiled_id != request.metadata.id:
        raise EvidenceError(
            f"intent id {request.metadata.id!r} does not match compiled "
            f"{handler.kind} {compiled_id!r} — refusing to emit mismatched evidence")

    scope = handler.scope_of(compiled)
    controls = list(BASE_CONTROLS)
    if risk.is_dual_control:
        controls.append(DUAL_CONTROL)

    md = request.metadata
    bundle: Dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "kind": handler.kind,
        "req_id": md.id,
        "status": status,
        "generated_at": _iso(generated_at),
        # PAPERWORK ONLY — see the module note. Anything describing what the
        # firewall will do belongs under `compiled`, which is derived from the
        # spec and so cannot silently disagree with it.
        "request": {
            "requester": md.requester,
            "ticket": md.ticket,
            "justification": md.justification,
            "requested": md.requested.isoformat(),
            "intent_file": intent_path,
            "intent_sha256": intent_sha256,
        },
        "compiled": {
            "compiler_version": __version__,
            "scope": {"kind": scope.kind, "value": scope.value},
            "object": _jsonable(handler.evidence_object(compiled)),
            "tfvars_file": handler.tfvars_filename,
            "tfvars_sha256": tfvars_sha256,
        },
        "risk": {
            "tier": risk.tier,
            "classifier_version": risk.classifier_version,
            "thresholds_version": risk.thresholds_version,
            "checks_fired": [dict(c) for c in risk.checks_fired],
        },
        "approval": {
            "gate": ci.gate,
            "approvers": list(ci.approvers),
            "pr": ci.pr_url,
            "merge_commit": ci.merge_commit,
        },
        "apply": {
            "plan_sha256": plan_sha256,
            "run_url": ci.run_url,
        },
        "push": push.to_evidence() if push is not None else None,
        "controls": controls,
    }
    if failure_reason:
        bundle["failure"] = {"reason": failure_reason}
    return bundle


def _jsonable(value: Any) -> Any:
    """Tuples -> lists, sets -> sorted lists, dates -> ISO, recursively.

    Serialising compiled dataclasses whole means the bundle inherits whatever
    types they use. `CompiledRoute.vrf_interfaces` is a tuple; `json.dumps`
    renders that as a list anyway, but normalising here keeps the in-memory
    bundle equal to the one a reader parses back, which is what the
    byte-stability test compares.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def dumps(bundle: Dict[str, Any]) -> str:
    """Byte-stable JSON so re-generating never churns Git."""
    return json.dumps(bundle, sort_keys=True, indent=2) + "\n"


def bundle_path(root: Path, bundle: Dict[str, Any]) -> Path:
    """`evidence/<scope-dirname>/<req-id>.json`.

    Keyed on SCOPE, not folder: a change targeting one firewall lands in
    `evidence/device-<serial>/`, mirroring the Terraform root layout. Reading the
    folder out of the bundle would have put a device-scoped change under a
    directory named for a serial that SCM rejects as a folder — the same
    folder-vs-device confusion that broke the drift job in v1.34.2.
    """
    from fwgitops.compiler import Scope

    scope = bundle["compiled"]["scope"]
    return Path(root) / Scope(**scope).dirname / f"{bundle['req_id']}.json"


def write_bundle(bundle: Dict[str, Any], root: Path) -> Path:
    target = bundle_path(root, bundle)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(bundle), encoding="utf-8")
    return target


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
