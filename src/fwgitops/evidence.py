"""NIST-mapped evidence bundles — one record per change, Git-resident.

The audit artifact that justifies the whole platform: an assessor or incident
responder can reconstruct **what changed, who authorised it, why, and what the
system checked** from the bundle alone — no CI logs, no ticket archaeology, no
SCM UI.

    intent + compiled change + risk verdict + approval + apply + push
                              │
                              ▼
              evidence/<folder>/<REQ-id>.json   (committed; Git = SSoT)

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from fwgitops import __version__
from fwgitops.compiler import CompiledChange
from fwgitops.intent import AccessRequest
from fwgitops.push import PushResult

EVIDENCE_SCHEMA = "fw-evidence/v1"

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
    request: AccessRequest,
    change: CompiledChange,
    status: str,
    generated_at: datetime,
    intent_sha256: Optional[str] = None,
    intent_path: Optional[str] = None,
    tfvars_sha256: Optional[str] = None,
    plan_sha256: Optional[str] = None,
    risk: RiskVerdict = RiskVerdict(),
    ci: CIContext = CIContext(),
    push: Optional[PushResult] = None,
    failure_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the evidence record for one change.

    `status` is applied / rejected / failed — failures are recorded, not dropped.
    """
    if status not in VALID_STATUSES:
        raise EvidenceError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if status in (STATUS_REJECTED, STATUS_FAILED) and not failure_reason:
        raise EvidenceError(f"status {status!r} requires a failure_reason")
    if request.metadata.id != change.rule.name:
        raise EvidenceError(
            f"intent id {request.metadata.id!r} does not match compiled rule "
            f"{change.rule.name!r} — refusing to emit mismatched evidence"
        )

    rule = change.rule
    controls = list(BASE_CONTROLS)
    if risk.is_dual_control:
        controls.append(DUAL_CONTROL)

    bundle: Dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "req_id": request.metadata.id,
        "status": status,
        "generated_at": _iso(generated_at),
        "request": {
            "requester": request.metadata.requester,
            "ticket": request.metadata.ticket,
            "justification": request.metadata.justification,
            "requested": request.metadata.requested.isoformat(),
            "expires": request.metadata.expires.isoformat() if request.metadata.expires else None,
            "environment": request.spec.environment,
            "action": request.spec.action,
            "intent_file": intent_path,
            "intent_sha256": intent_sha256,
        },
        "compiled": {
            "compiler_version": __version__,
            "folder": rule.folder,
            "address_objects": sorted(o.name for o in change.address_objects),
            "service_objects": sorted(o.name for o in change.service_objects),
            "rule": {
                "name": rule.name,
                "from_zones": list(rule.from_zones),
                "to_zones": list(rule.to_zones),
                "sources": list(rule.sources),
                "destinations": list(rule.destinations),
                "services": list(rule.services),
                "action": rule.action,
                "log_end": rule.log_end,
                # ADR-0003 enrichment — the effective rule an assessor sees. These
                # are set on-device by `fwgitops enrich` (the scm provider drops
                # them); recording them from the compiled desired-state makes the
                # bundle the full audit record, not just the skeleton.
                "application": list(rule.application),
                "profile_group": rule.profile_group,
                "log_setting": rule.log_setting,
                "rulebase": rule.rulebase,
                "relative_position": rule.relative_position,
                "target_rule": rule.target_rule,
            },
            "tags": list(rule.tags),
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


def dumps(bundle: Dict[str, Any]) -> str:
    """Byte-stable JSON so re-generating never churns Git."""
    return json.dumps(bundle, sort_keys=True, indent=2) + "\n"


def bundle_path(root: Path, bundle: Dict[str, Any]) -> Path:
    folder = bundle["compiled"]["folder"]
    return Path(root) / folder / f"{bundle['req_id']}.json"


def write_bundle(bundle: Dict[str, Any], root: Path) -> Path:
    target = bundle_path(root, bundle)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(bundle), encoding="utf-8")
    return target


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
