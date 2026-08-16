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
CM-3 (request → review → approve → implement), AU-2 / AU-12 (this record IS the
audit record), SC-7 (enforced boundary). Those hold from the record's own
contents, whatever CI knew.

CONDITIONAL, because a listed control is a claim it was OPERATING:
  * CM-5 (who may approve vs who did) — only when an APPROVER IS NAMED. It was
    unconditional until v1.38.0 while `approvers` was hard-coded `()` and
    `pr_url` `None`, with no caller passing either, so every bundle claimed it
    and evidenced nobody. Absent, it is omitted AND the omission is named in
    `controls_not_evidenced`: a silently shorter list reads as an older schema
    rather than a gap.
  * AC-5 (separation of duties) — dual-control, CRITICAL tier.
"""

from __future__ import annotations

import re

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fwgitops import __version__
from fwgitops.push import PushResult

EVIDENCE_SCHEMA = "fw-evidence/v2"

#: Controls every change record supports from its own contents — the intent, the
#: compiled object, the risk verdict and this record's existence are all present
#: whatever CI knew.
BASE_CONTROLS: Tuple[str, ...] = ("AC-4", "CM-3", "AU-2", "AU-12", "SC-7")
#: NOT baseline. CM-5 is "access restrictions for change" — who MAY approve
#: versus who DID — so it is evidenced by naming an approver, and by nothing
#: else. It was listed unconditionally until v1.38.0 while `approvers` was
#: hard-coded to `()` and `pr` to `None`: `CIContext.from_env` never read either,
#: and no caller passed them, so EVERY bundle ever produced claimed a control it
#: carried no evidence for. Claimed-but-empty is worse than absent — an assessor
#: reads the claim, not the empty list beside it.
APPROVAL_CONTROL = "CM-5"
#: Added when the change went through dual-control approval (CRITICAL tier).
DUAL_CONTROL = "AC-5"

STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
#: The object was destroyed in SCM AND the push delivering it succeeded — the
#: same bar `applied` meets, deliberately. A destroy whose push is refused is
#: `failed`, not `removed`: ADR-0008 measured exactly that during the route test,
#: with SCM reporting no default route while the device still forwarded on one.
STATUS_REMOVED = "removed"
VALID_STATUSES = (STATUS_APPLIED, STATUS_REJECTED, STATUS_FAILED, STATUS_REMOVED)


class EvidenceError(Exception):
    """The bundle could not be built (missing or inconsistent inputs)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


#: How an approval was given. Kept apart because they are different controls in
#: practice: reviewing the PROPOSED CHANGE is not the same act as releasing the
#: DEPLOYMENT, and the same person doing both is a finding, not a detail.
VIA_REVIEW = "pull_request_review"
VIA_DEPLOYMENT = "deployment_gate"


@dataclass(frozen=True)
class Approver:
    """Who approved, and which gate they exercised."""

    login: str
    via: str

    def to_evidence(self) -> Dict[str, str]:
        return {"login": self.login, "via": self.via}

    @classmethod
    def parse(cls, spec: str) -> "Approver":
        """`login:via`, the CLI form. A bare login is an unattributed approval.

        Unattributed rather than defaulted to a gate: guessing which restriction
        was exercised is exactly the kind of invented detail this record must not
        contain.
        """
        login, _, via = spec.partition(":")
        return cls(login=login.strip(), via=(via.strip() or "unspecified"))


@dataclass(frozen=True)
class CIContext:
    """Facts only the CI run knows. Passed in, never discovered.

    `approvers` and `pr_url` are the reason this class exists, and were the two
    fields nothing ever filled. They cannot be discovered here: the approvals
    live behind the GitHub API, and reaching for them would put a network call
    inside the record builder. The workflow fetches them and passes them in.
    """

    pr_url: Optional[str] = None
    merge_commit: Optional[str] = None
    run_url: Optional[str] = None
    gate: Optional[str] = None
    approvers: Tuple[Approver, ...] = ()

    def __post_init__(self) -> None:
        """Coerce `approvers` however this was constructed.

        `from_env` is not the only door — callers build a CIContext directly, and
        a bare `("alice",)` would otherwise sail through and fail at
        SERIALISATION, long after the run that could have explained it. Normalise
        at the boundary so the type invariant is true of every instance.
        """
        object.__setattr__(self, "approvers", tuple(
            a if isinstance(a, Approver) else Approver.parse(str(a))
            for a in (self.approvers or ())))

    @property
    def has_approval_evidence(self) -> bool:
        """Is there a NAMED approver? A protected environment is not enough.

        `gate` is only the environment's name. It says a restriction was
        configured, not that a human exercised it — and CM-5 is about who did.
        """
        return bool(self.approvers)

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
            # Read from env like everything else here. Hard-coding this to None
            # while the bundle claimed CM-5 is how the control stayed unevidenced
            # for eight releases — there was no code path that could have filled
            # it, and nothing said so.
            pr_url=env.get("GITHUB_PR_URL") or None,
            merge_commit=env.get("GITHUB_SHA") or None,
            run_url=run_url,
            gate=env.get("GITHUB_ENVIRONMENT") or None,
            approvers=(),
        )
        base.update({k: v for k, v in overrides.items() if v is not None})
        # No coercion here: __post_init__ owns it, so every construction path
        # gets the same treatment rather than only this one.
        base["approvers"] = tuple(base.get("approvers") or ())
        return cls(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RemovalContext:
    """What authorises a REMOVAL, as opposed to what authorised the object.

    A modified intent proves its own authorisation — `stale_ticket_problems`
    requires `metadata.ticket` to move with the spec. A removal cannot, because
    the fix is deleting the file, so there is nowhere left to write the new
    ticket. Without this, the record for an August deletion would carry the July
    ticket that authorised CREATING the object: the same false CM-3 statement,
    reached through deletion instead of modification.

    So on a `removed` record the two are SEPARATE and both are kept:
    `request.ticket` is what asked for the object, `removal.ticket` is what
    asked for it to go.
    """

    ticket: str
    commit: Optional[str] = None


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
    removal: Optional[RemovalContext] = None,
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
    # A removal MUST name what authorised it. Emitting one without would leave the
    # record carrying only the ticket that authorised creating the object — the
    # exact misattribution `RemovalContext` exists to prevent, so it fails rather
    # than degrades.
    if status == STATUS_REMOVED and (removal is None or not removal.ticket):
        raise EvidenceError(
            f"status {STATUS_REMOVED!r} requires a RemovalContext with a ticket — the "
            f"intent's own ticket authorised CREATING the object, not removing it")
    if removal is not None and status != STATUS_REMOVED:
        raise EvidenceError(
            f"a RemovalContext is only meaningful on status {STATUS_REMOVED!r}, got {status!r}")

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
    # CONTROLS ARE EVIDENCED, NOT ASSUMED. A control listed here is a claim that
    # it was OPERATING for this change, so one the record cannot substantiate is
    # omitted — and the omission is NAMED, because a silently shorter list reads
    # as an older schema rather than a gap.
    controls = list(BASE_CONTROLS)
    not_evidenced: List[Dict[str, str]] = []
    if ci.has_approval_evidence:
        controls.append(APPROVAL_CONTROL)
    else:
        not_evidenced.append({
            "control": APPROVAL_CONTROL,
            "why": "no approver was recorded for this change. CM-5 is about WHO "
                   "approved, and this run passed none — either the workflow did "
                   "not collect them, or nothing required an approval.",
        })
    if risk.is_dual_control:
        controls.append(DUAL_CONTROL)

    md = request.metadata
    obj = _jsonable(handler.evidence_object(compiled))
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
            "object": obj,
            # THIS REQUEST'S OWN contribution, hashed. `tfvars_sha256` below is
            # the whole FILE, which several requests share — every rule in a
            # folder writes `rules.auto.tfvars.json`, and every route for a VRF
            # aggregates into one router — so the file hash moves when a
            # neighbour changes and says nothing about this request. The object
            # hash is what "did this change?" actually means here.
            "object_sha256": sha256_bytes(_canonical(obj)),
            "tfvars_file": handler.tfvars_filename,
            #: The whole file, SHARED with other requests in the same scope.
            "tfvars_sha256": tfvars_sha256,
        },
        "risk": {
            "tier": risk.tier,
            "classifier_version": risk.classifier_version,
            "thresholds_version": risk.thresholds_version,
            "checks_fired": [dict(c) for c in risk.checks_fired],
        },
        "approval": {
            # The environment NAME — a restriction was configured. Not proof one
            # was exercised; `approvers` is that.
            "gate": ci.gate,
            "approvers": [a.to_evidence() for a in ci.approvers],
            "pr": ci.pr_url,
            "merge_commit": ci.merge_commit,
        },
        "apply": {
            "plan_sha256": plan_sha256,
            "run_url": ci.run_url,
        },
        "push": push.to_evidence() if push is not None else None,
        "controls": controls,
        "controls_not_evidenced": not_evidenced,
    }
    if removal is not None:
        # SEPARATE from `request` on purpose. `request.*` describes the change
        # being withdrawn; these describe the withdrawal. Merging them is what
        # would let a deletion inherit the creation's authorisation.
        bundle["removal"] = {
            "ticket": removal.ticket,
            "commit": removal.commit,
            "authorises": "the removal — `request.ticket` authorised the object itself",
        }
        # The object is gone, so there is no tfvars file left to hash for it. Say
        # so rather than leaving a null that reads like a failed lookup.
        bundle["compiled"]["tfvars_sha256"] = None
        bundle["compiled"]["object_is"] = "the LAST APPLIED state, from the baseline tree"
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
    """Write unconditionally. Prefer `write_bundle_if_changed` on the apply path."""
    target = bundle_path(root, bundle)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(bundle), encoding="utf-8")
    return target


#: What makes two bundles records of the SAME change. Everything outside this
#: set — `generated_at`, the CI run, the risk verdict — describes the RUN or the
#: RULESET, not the change, and must not by itself rewrite an existing record.
_IDENTITY = (
    ("schema",),
    ("kind",),
    ("status",),
    ("request", "intent_sha256"),
    ("compiled", "object_sha256"),
)


def _at(bundle: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    cur: Any = bundle
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def describes_same_change(new: Dict[str, Any], old: Dict[str, Any]) -> bool:
    """Do these two bundles record the same change, applied for the same reason?

    Deliberately EXCLUDES the risk verdict. The bundle records the decision that
    gated THIS apply; a later classifier re-tiering config nobody touched is a
    real question, but it is policy drift and belongs in its own report, not in a
    change record backdated to an apply that never re-evaluated it.
    """
    return all(_at(new, p) == _at(old, p) for p in _IDENTITY)


def write_bundle_if_changed(bundle: Dict[str, Any], root: Path) -> Tuple[Path, bool]:
    """Write only when this is a NEW change. Returns (path, written).

    WHY THIS EXISTS. `fwgitops evidence` regenerates every bundle on every apply,
    and `generated_at` always moves, so every bundle differed from its committed
    version and the workflow committed all of them — each stamped with that run's
    `run_url` and `merge_commit`. A record for a request nobody touched claimed
    to have been applied by a run that applied something else. That is the CM-3
    misattribution already fixed for stale tickets, arriving through the writer
    instead of the intent.

    It also broke a property this project states out loud, in
    `test_evidence_durability`: *one file per request, so each commit to it is
    one change, carrying the ticket that authorised it*. With every apply
    rewriting every file, `git log evidence/<scope>/<REQ>.json` was a log of
    APPLIES, not of changes to that request — the audit question it exists to
    answer.

    So an unchanged bundle is left EXACTLY as committed, byte for byte. The
    record keeps the run that actually made the change.
    """
    target = bundle_path(root, bundle)
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None      # unreadable: rewrite rather than preserve garbage
        if isinstance(existing, dict) and describes_same_change(bundle, existing):
            return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(bundle), encoding="utf-8")
    return target, True


def _canonical(value: Any) -> bytes:
    """Byte-stable serialisation for hashing. Same ordering rule as `dumps`."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Manual actions ─────────────────────────────────────────────────────────
#
# An object this platform never created cannot be removed through the pipeline:
# Git has no representation of it, so there is nothing to delete FROM Git
# (ADR-0011). The removal is out-of-band by necessity — but auditable is
# available, and auditable is what the source-of-truth rule actually buys here.

#: v2 ADDS A REQUIRED FIELD, so it is a new version rather than a v1 that
#: sometimes carries `provenance`. A reader that trusts v1 must not silently
#: accept a record whose authorship it cannot see.
MANUAL_ACTION_SCHEMA = "fw-manual-action/v2"

#: HOW THE RECORD CAME TO EXIST — see `build_manual_action`.
PROVENANCE = ("workflow", "reconstructed")

#: Anything outside this becomes `_` in a record FILENAME. SCM object names may
#: contain spaces, slashes and dots; a slash would silently write the record
#: into a directory that does not exist, and the shipped version did exactly
#: that until 2026-08-16.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def build_manual_action(*, action: str, kind: str, folder: str, name: str,
                        object_id: Optional[str], tags: Sequence[str],
                        reason: str, actor: str, run_url: str,
                        provenance: str, violation_id: Optional[str],
                        unlinked_reason: str = "", reconstructed_from: str = "",
                        at: Optional[str] = None) -> Dict[str, Any]:
    """A record of something done to SCM directly, outside the pipeline.

    DELIBERATELY NOT AN EVIDENCE BUNDLE. Those are keyed on a request id from an
    intent, and an unmanaged object has none — borrowing that shape would imply
    an authorisation that never existed. This says what it is: a manual action,
    with who did it and why.

    `provenance` IS REQUIRED AND HAS NO DEFAULT, which is the point of it.

    This directory's whole value is that a machine wrote it. On 2026-08-16 a
    deletion succeeded and the record step crashed, so the only record of an
    irreversible act was reconstructed BY HAND from a CI log — sitting in that
    directory, distinguished from a genuine record by a free-text field nothing
    validated. Convention, not structure.

    Defaulting this to "workflow" would have been worse than leaving it out: a
    reconstruction that simply forgot to say so would then CLAIM machine
    authorship, which is the exact confusion the field exists to prevent. No
    default means the accidental path does not exist — someone writing a
    reconstruction has to type the word. Nothing here can stop a person stating
    it falsely, and this does not pretend to: it removes the accident, not the
    lie.

    A `reconstructed` record must also say WHAT it was rebuilt from — a record
    that cannot be traced to a source is not a reconstruction, it is an
    assertion.

    `violation_id` LINKS THE ACT TO WHAT JUSTIFIED IT, and is required for the
    same reason. Until 2026-08-16 that link existed only as prose someone typed
    into `reason`, so "show me every unauthorised change and what removed it"
    was a manual read of two directories. With the id on the record it is a
    join.

    Pass `None` when the deletion remediates no DETECTED violation — a disposable
    fixture, a cleanup — but then `unlinked_reason` must say why. Deleting
    something the platform never flagged is not wrong, and it is exactly the case
    worth stating out loud rather than leaving as an empty field.
    """
    if provenance not in PROVENANCE:
        raise ValueError(
            f"provenance must be one of {PROVENANCE}, got {provenance!r}. Say how "
            f"this record came to exist — a directory of machine-written records "
            f"is only worth what the weakest claim in it is worth.")
    if provenance == "reconstructed" and not reconstructed_from.strip():
        raise ValueError(
            "a reconstructed record must name what it was rebuilt from (a run "
            "URL, a log, a console export). Without a source it is an assertion, "
            "not a reconstruction.")
    if violation_id is None and not unlinked_reason.strip():
        raise ValueError(
            "a deletion with no violation_id must say why in `unlinked_reason` "
            "— 'nothing detected this' is a claim worth recording, not an empty "
            "field")
    if violation_id is not None and not str(violation_id).startswith("VIOL-"):
        raise ValueError(
            f"violation_id {violation_id!r} does not look like a violation id "
            f"(VIOL-YYYY-MMDD-scope-name); a link that resolves to nothing is "
            f"worse than no link")
    rec = {
        "schema": MANUAL_ACTION_SCHEMA,
        "action": action,
        "violation_id": violation_id,
        "kind": kind,
        "folder": folder,
        "name": name,
        "object_id": object_id,
        "tags_at_deletion": list(tags),
        "reason": reason,
        "dispatched_by": actor,
        "run_url": run_url,
        "provenance": provenance,
        "at": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if provenance == "reconstructed":
        rec["reconstructed_from"] = reconstructed_from
    if violation_id is None:
        rec["unlinked_reason"] = unlinked_reason
    return rec


def validate_manual_action(record: Dict[str, Any]) -> None:
    """Raise unless the record states, structurally, how it came to exist.

    Enforced at WRITE time rather than left to callers: a record that reaches
    disk without this has already become evidence.
    """
    prov = record.get("provenance")
    if prov not in PROVENANCE:
        raise ValueError(
            f"manual-action record for {record.get('name')!r} has provenance "
            f"{prov!r}; expected one of {PROVENANCE}")
    if prov == "reconstructed" and not str(record.get("reconstructed_from", "")).strip():
        raise ValueError(
            f"reconstructed record for {record.get('name')!r} names no source")
    if "violation_id" not in record:
        raise ValueError(
            f"manual-action record for {record.get('name')!r} does not say which "
            f"violation it remediates (use null plus `unlinked_reason` if none)")
    if record["violation_id"] is None and not str(
            record.get("unlinked_reason", "")).strip():
        raise ValueError(
            f"manual-action record for {record.get('name')!r} links to no "
            f"violation and does not say why")


def manual_action_path(record: Dict[str, Any], root: Path) -> Path:
    """Where a manual-action record lands. The name is SANITISED — see
    `_UNSAFE_IN_FILENAME`."""
    stamp = str(record["at"]).replace(":", "").replace("-", "")
    safe = _UNSAFE_IN_FILENAME.sub("_", str(record["name"])).strip("._-") or "object"
    return Path(root) / "manual-actions" / f"{stamp}-{record['kind']}-{safe}.json"


def write_manual_action(record: Dict[str, Any], root: Path) -> Path:
    validate_manual_action(record)      # a record on disk is already evidence
    out = manual_action_path(record, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
