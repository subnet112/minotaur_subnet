"""Distributed benchmark veto — protocol logic (Phase 0, observe-only).

PURE module: dataclasses, canonical serialization, deterministic assignment,
phase resolution, dissent re-verification planning, audit selection. No I/O,
no crypto, no stores — everything here is unit-testable in isolation and the
coordinator/route layers stay thin (the ``autoscaled_decision_window``
extraction precedent: startup.py closures are untestable).

Protocol shape (see the design record for the full trust analysis):
- The leader's canonical draw (synthetics + ``sample_historical_orders``)
  remains the SOLE adoption corpus. Each reachable follower is assigned a
  DISJOINT remainder slice (``order_sampler.partition_follower_slices``) and
  benches champion + candidate on it at the round's fork pins.
- Followers can only VETO — per-order HARD violations (catastrophic >1% cut,
  dropped order), each single-order-verifiable. The leader re-benchmarks the
  claimed orders itself before honoring a veto (trust-but-verify): a veto
  stands ONLY on a leader-reproduced violation, so a false veto cannot reject
  a challenger and a fabricated win cannot adopt one (follower counts never
  enter net_better).
- Liveness inversion: absence of evidence ⇒ proceed. Non-response, FAILED,
  REFUSED, invalid signature and UNSUPPORTED endpoints are all ABSTAIN; zero
  assignments resolves the phase synchronously in the same coordinator tick.
  A round can never fail because a third-party validator misbehaved.

Slice→validator assignment and audit selection are seeded with POST-CLOSE
entropy (a close-block hash) — NOT round_id or committee_hash, which are both
fully predictable in advance (round_id = ``round-e{epoch}-n{count}``;
committee_hash = keccak of the sorted validator set). Predictable assignment
would let a challenger operator pre-position a submission into its own
colluding validator's slice.

Consensus-relevant constants are CODE constants, never envs (the
``order_sampler.STAGE2_CORPUS_SAMPLES`` precedent).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Max orders the leader re-benchmarks per dissenting response, dropped-first
# then largest claimed cut, short-circuiting on the first leader-confirmed
# violation. Bounds the DoS-by-dissent surface (a 50-violation claim costs the
# leader at most this many order-benches before the veto is discarded).
VETO_VERIFY_BUDGET_ORDERS: int = 10

# Orders re-benchmarked from one responder's slice per round as a lazy/malicious
# OK deterrent (deterrence is the point; the leader's own corpus still gates).
VETO_AUDIT_ORDERS: int = 3

# Ingestion caps — enforced BEFORE any response content is trusted, so a
# malicious follower cannot bloat the leader's round state (the 142MB
# submissions.json event-loop freeze is the incident class this guards).
MAX_VIOLATIONS_PER_RESPONSE: int = 64
MAX_CALIBRATION_ROWS: int = 16

VIOLATION_CATASTROPHIC = "catastrophic"
VIOLATION_DROPPED = "dropped"
_VIOLATION_KINDS = (VIOLATION_CATASTROPHIC, VIOLATION_DROPPED)

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"
_RESPONSE_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_REFUSED)

VERDICT_OK = "ok"
VERDICT_VETO = "veto"

RESOLUTION_NO_ASSIGNMENTS = "no_assignments"
RESOLUTION_ALL_TERMINAL = "all_terminal"
RESOLUTION_WINDOW_ELAPSED = "window_elapsed"

ACTION_WAIT = "wait"
ACTION_RESOLVE = "resolve"
ACTION_NOOP = "noop"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Wire/state dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SliceViolation:
    """One claimed per-order hard violation, with the exact-wei evidence.

    The values are CLAIMS — never trusted numerically. The leader re-benches
    the order itself and recomputes the verdict with the same relative-rule
    constants; these fields exist so the claim is auditable and so the
    re-verification planner can order work by claimed severity.
    """

    order_id: str
    kind: str  # catastrophic | dropped
    champ_raw: str  # exact decimal wei string, as the relative rule consumes
    chal_raw: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "kind": self.kind,
            "champ_raw": self.champ_raw,
            "chal_raw": self.chal_raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SliceViolation":
        return cls(
            order_id=str(d.get("order_id", "")),
            kind=str(d.get("kind", "")),
            champ_raw=str(d.get("champ_raw", "")),
            chal_raw=str(d.get("chal_raw", "")),
        )


def violation_severity_key(v: SliceViolation) -> tuple:
    """Sort key: dropped orders first, then largest claimed cut (exact integer
    bps — no floats, same discipline as the relative rule), order_id tiebreak."""
    if v.kind == VIOLATION_DROPPED:
        return (0, 0, v.order_id)
    try:
        champ = int(v.champ_raw)
        chal = int(v.chal_raw)
    except (TypeError, ValueError):
        return (1, 0, v.order_id)
    cut_bps = ((champ - chal) * 10_000 // champ) if champ > 0 else 0
    return (1, -cut_bps, v.order_id)


@dataclass
class SliceAssignment:
    """One signed work order: bench champion + candidate on this slice.

    Everything a follower needs is IN the assignment — incumbent digest, fork
    pins, explicit order ids + replay hashes — so a drifted follower REFUSES
    loudly instead of benching the wrong champion, the wrong block, or the
    wrong orders. ``fork_pins`` keys are stringified chain ids (JSON-safe);
    a missing pin for a slice order's chain is a REFUSED, never a live-head
    fallback.
    """

    round_id: str
    slice_index: int
    validator_evm: str  # lowercase 0x…
    candidate_submission_id: str
    candidate_image_id: str  # bare GHCR digest (pull-by-digest is the verifier)
    incumbent_image_id: str
    order_ids: list[str]
    order_hashes: dict[str, str]  # order_id -> order_replay_hash
    calibration_order_ids: list[str]
    fork_pins: dict[str, int]  # str(chain_id) -> block
    deadline_epoch: int

    @property
    def slice_hash(self) -> str:
        lines = [f"{oid}:{self.order_hashes.get(oid, '')}" for oid in self.order_ids]
        return _sha256_hex("\n".join(lines))

    @property
    def assignment_id(self) -> str:
        """Binds round + candidate + slice content. The response idempotency key
        is (round_id, assignment_id, validator): a stale response for a previous
        candidate in the same round can never be misread as a fresh verdict."""
        return _sha256_hex(_canonical({
            "round_id": self.round_id,
            "candidate_image_id": self.candidate_image_id,
            "slice_hash": self.slice_hash,
        }))[:32]

    def to_payload(self) -> dict[str, Any]:
        """Canonical wire dict — what gets leader-signed and POSTed."""
        return {
            "round_id": self.round_id,
            "slice_index": self.slice_index,
            "validator_evm": self.validator_evm.lower(),
            "candidate_submission_id": self.candidate_submission_id,
            "candidate_image_id": self.candidate_image_id,
            "incumbent_image_id": self.incumbent_image_id,
            "order_ids": list(self.order_ids),
            "order_hashes": dict(self.order_hashes),
            "calibration_order_ids": list(self.calibration_order_ids),
            "fork_pins": dict(self.fork_pins),
            "deadline_epoch": self.deadline_epoch,
            "slice_hash": self.slice_hash,
            "assignment_id": self.assignment_id,
        }

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> "SliceAssignment":
        return cls(
            round_id=str(d.get("round_id", "")),
            slice_index=int(d.get("slice_index", -1)),
            validator_evm=str(d.get("validator_evm", "")).lower(),
            candidate_submission_id=str(d.get("candidate_submission_id", "")),
            candidate_image_id=str(d.get("candidate_image_id", "")),
            incumbent_image_id=str(d.get("incumbent_image_id", "")),
            order_ids=[str(x) for x in (d.get("order_ids") or [])],
            order_hashes={
                str(k): str(v) for k, v in (d.get("order_hashes") or {}).items()
            },
            calibration_order_ids=[
                str(x) for x in (d.get("calibration_order_ids") or [])
            ],
            fork_pins={
                str(k): int(v) for k, v in (d.get("fork_pins") or {}).items()
            },
            deadline_epoch=int(d.get("deadline_epoch", 0)),
        )


def verify_assignment_integrity(payload: dict[str, Any]) -> tuple[bool, str]:
    """Recompute slice_hash + assignment_id from the payload fields and compare
    with the transmitted values — cheap tamper/corruption check the follower
    runs BEFORE validating order content against its own store."""
    try:
        assignment = SliceAssignment.from_payload(payload)
    except (TypeError, ValueError) as exc:
        return False, f"malformed assignment: {exc}"
    if payload.get("slice_hash") != assignment.slice_hash:
        return False, "slice_hash mismatch"
    if payload.get("assignment_id") != assignment.assignment_id:
        return False, "assignment_id mismatch"
    if not assignment.order_ids:
        return False, "empty slice"
    if set(assignment.order_hashes) != set(assignment.order_ids):
        return False, "order_hashes keys != order_ids"
    return True, ""


@dataclass
class VetoResponse:
    """A follower's signed verdict for ONE assignment.

    ``status`` semantics: completed carries a verdict (ok | veto); failed and
    refused are TERMINAL ABSTAINS (the follower answered, the answer is "no
    evidence"), with ``error`` naming the cause (corpus_mismatch, no_pin,
    missing_app, bench_error, …) for the observability surface.
    """

    assignment_id: str
    round_id: str
    validator_evm: str
    status: str  # completed | failed | refused
    verdict: str | None = None  # ok | veto (completed only)
    violations: list[SliceViolation] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    calibration: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "round_id": self.round_id,
            "validator_evm": self.validator_evm.lower(),
            "status": self.status,
            "verdict": self.verdict,
            "violations": [v.to_dict() for v in self.violations],
            "counts": dict(self.counts),
            "calibration": [dict(r) for r in self.calibration],
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> "VetoResponse":
        return cls(
            assignment_id=str(d.get("assignment_id", "")),
            round_id=str(d.get("round_id", "")),
            validator_evm=str(d.get("validator_evm", "")).lower(),
            status=str(d.get("status", "")),
            verdict=(None if d.get("verdict") is None else str(d.get("verdict"))),
            violations=[
                SliceViolation.from_dict(x) for x in (d.get("violations") or [])
            ],
            counts={
                str(k): int(v) for k, v in (d.get("counts") or {}).items()
            },
            calibration=[
                {str(k): str(v) for k, v in (r or {}).items()}
                for r in (d.get("calibration") or [])
            ],
            error=(None if d.get("error") is None else str(d.get("error"))),
        )


def validate_response(
    response: VetoResponse, assignment: SliceAssignment,
) -> tuple[bool, str]:
    """Structural + binding validation of a response against ITS assignment.

    Signature verification and size-capped ingestion happen at the route layer
    BEFORE this; here we bind identity and bound content. Anything invalid is
    an ABSTAIN, never an error the round can trip on.
    """
    if response.assignment_id != assignment.assignment_id:
        return False, "assignment_id mismatch"
    if response.round_id != assignment.round_id:
        return False, "round_id mismatch"
    if response.validator_evm.lower() != assignment.validator_evm.lower():
        return False, "validator mismatch"
    if response.status not in _RESPONSE_STATUSES:
        return False, f"unknown status {response.status!r}"
    if response.status == STATUS_COMPLETED:
        if response.verdict not in (VERDICT_OK, VERDICT_VETO):
            return False, f"completed response with verdict {response.verdict!r}"
    elif response.verdict is not None:
        return False, "verdict on a non-completed response"
    if len(response.violations) > MAX_VIOLATIONS_PER_RESPONSE:
        return False, "too many violations"
    if len(response.calibration) > MAX_CALIBRATION_ROWS:
        return False, "too many calibration rows"
    # counts/error are small scalar fields; the route layer's Content-Length
    # reject is the size bound for everything not list-capped here.
    if response.violations and response.verdict != VERDICT_VETO:
        return False, "violations on a non-veto response"
    if response.verdict == VERDICT_VETO and not response.violations:
        # A veto with no evidence is unverifiable by construction — reject at
        # ingestion instead of letting it reach the planner as a phantom OK.
        return False, "veto with no violations"
    slice_ids = set(assignment.order_ids)
    for v in response.violations:
        if v.kind not in _VIOLATION_KINDS:
            return False, f"unknown violation kind {v.kind!r}"
        if v.order_id not in slice_ids:
            return False, f"violation references order outside the slice: {v.order_id}"
    calib_ids = set(assignment.calibration_order_ids)
    for row in response.calibration:
        if row.get("order_id") not in calib_ids:
            return False, "calibration row references a non-calibration order"
    return True, ""


@dataclass
class VetoPhaseState:
    """Per-round veto phase, persisted on the RoundState (BOUNDED — ids, hashes
    and counts only; per-order result rows never land on the round record).

    Assignments are persisted with their full order-id lists (~2KB/slice) so a
    leader restart re-serves the ORIGINAL assignments verbatim: re-deriving
    from the live order store diverges (the corpus keeps growing after close),
    which would orphan every in-flight follower response.
    """

    candidate_submission_id: str
    candidate_image_id: str
    deadline_epoch: int
    assignments: list[SliceAssignment] = field(default_factory=list)
    responses: dict[str, VetoResponse] = field(default_factory=dict)  # by evm
    unsupported: list[str] = field(default_factory=list)  # evms: terminal 404s
    resolved: bool = False
    resolution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_submission_id": self.candidate_submission_id,
            "candidate_image_id": self.candidate_image_id,
            "deadline_epoch": self.deadline_epoch,
            "assignments": [a.to_payload() for a in self.assignments],
            "responses": {
                evm: r.to_payload() for evm, r in sorted(self.responses.items())
            },
            "unsupported": sorted(self.unsupported),
            "resolved": self.resolved,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VetoPhaseState":
        return cls(
            candidate_submission_id=str(d.get("candidate_submission_id", "")),
            candidate_image_id=str(d.get("candidate_image_id", "")),
            deadline_epoch=int(d.get("deadline_epoch", 0)),
            assignments=[
                SliceAssignment.from_payload(a) for a in (d.get("assignments") or [])
            ],
            responses={
                str(evm).lower(): VetoResponse.from_payload(r)
                for evm, r in (d.get("responses") or {}).items()
            },
            unsupported=[str(x).lower() for x in (d.get("unsupported") or [])],
            resolved=bool(d.get("resolved", False)),
            resolution=(
                None if d.get("resolution") is None else str(d.get("resolution"))
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic assignment / audit selection (post-close entropy)
# ─────────────────────────────────────────────────────────────────────────────

def assign_slices(
    validator_evms: list[str], n_slices: int, entropy: str,
) -> dict[str, int]:
    """Map validators → slice indices, ranked by sha256(entropy || evm).

    ``entropy`` MUST be post-close (a close-block hash): with pre-close-known
    seeds (round_id, committee_hash — both fully predictable) a challenger
    operator could pre-position a submission into its own validator's slice.
    Deterministic given (validators, entropy), so the persisted assignment can
    be re-derived and audited. Validators beyond ``n_slices`` get no slice this
    round (coverage is opportunistic).
    """
    unique = sorted({evm.lower() for evm in validator_evms if evm})
    ranked = sorted(unique, key=lambda evm: _sha256_hex(f"{entropy}|{evm}"))
    return {evm: idx for idx, evm in enumerate(ranked[: max(0, n_slices)])}


def pick_audit_target(responder_evms: list[str], entropy: str) -> str | None:
    """One responding validator to audit this round — post-close-entropy-seeded
    AND round-scoped, so no validator can know in advance it will never be
    audited (the fixed-target failure of a committee-hash-only seed)."""
    unique = sorted({evm.lower() for evm in responder_evms if evm})
    if not unique:
        return None
    return min(unique, key=lambda evm: _sha256_hex(f"{entropy}|audit|{evm}"))


def pick_audit_orders(
    order_ids: list[str], entropy: str, n: int = VETO_AUDIT_ORDERS,
) -> list[str]:
    """Deterministic audit subset of one slice (rank by keyed hash — no RNG
    state to reproduce)."""
    ranked = sorted(order_ids, key=lambda oid: _sha256_hex(f"{entropy}|order|{oid}"))
    return ranked[: max(0, n)]


# ─────────────────────────────────────────────────────────────────────────────
# Phase resolution + re-verification planning (pure decisions)
# ─────────────────────────────────────────────────────────────────────────────

def response_is_terminal(response: VetoResponse | None) -> bool:
    return response is not None and response.status in _RESPONSE_STATUSES


def resolve_phase(state: VetoPhaseState, current_epoch: int) -> tuple[str, str | None]:
    """Decide the phase's next step. Returns (action, resolution).

    - ``("noop", None)`` — already resolved.
    - ``("resolve", "no_assignments")`` — ZERO assignments were issued: resolve
      synchronously in the SAME coordinator tick, no deadline wait. This is the
      liveness invariant that keeps an empty/unreachable fleet at exactly
      today's behavior (quorum-1 must never stall on a phase with no work).
    - ``("resolve", "all_terminal")`` — every assignment has a terminal response
      or its validator is marked UNSUPPORTED: early-resolve, don't pay the
      deadline (without this, every clean round would idle out the full
      worst-case window and champion activation latency would triple).
    - ``("resolve", "window_elapsed")`` — deadline passed: resolve with what
      arrived; outstanding assignments are abstains.
    - ``("wait", None)`` — responses outstanding, deadline not reached.

    The veto deadline MUST be strictly interior to the round's decision
    deadline (with margin for re-verification) — the coordinator's expired-
    round reaper covers CERTIFYING, and this function never sees that clock.
    """
    if state.resolved:
        return ACTION_NOOP, None
    if not state.assignments:
        return ACTION_RESOLVE, RESOLUTION_NO_ASSIGNMENTS
    unsupported = {evm.lower() for evm in state.unsupported}

    def _terminal(a: SliceAssignment) -> bool:
        if a.validator_evm.lower() in unsupported:
            return True
        r = state.responses.get(a.validator_evm.lower())
        # Bind to THIS assignment: a terminal response for a different
        # assignment_id (stale candidate, restored state with multiple slices
        # per validator) must not early-resolve someone else's outstanding work.
        return (
            response_is_terminal(r) and r.assignment_id == a.assignment_id
        )

    if all(_terminal(a) for a in state.assignments):
        return ACTION_RESOLVE, RESOLUTION_ALL_TERMINAL
    if current_epoch > state.deadline_epoch:
        return ACTION_RESOLVE, RESOLUTION_WINDOW_ELAPSED
    return ACTION_WAIT, None


def plan_reverification(
    response: VetoResponse, budget: int = VETO_VERIFY_BUDGET_ORDERS,
) -> list[SliceViolation]:
    """Order a dissent's claimed violations for leader re-benchmarking.

    Dropped orders first (cheapest to confirm, hardest to fake), then largest
    claimed cut — exact integer bps, no floats. Capped at ``budget``: the
    leader verifies until the FIRST reproduced violation (veto stands) or the
    budget exhausts with zero confirmations (veto discarded + strike). The
    caller must ALSO time-box against the remaining decision window: a dissent
    arriving near the deadline must degrade to unverifiable-abstain, never
    into a certification_deadline_elapsed abort.
    """
    if response.verdict != VERDICT_VETO or not response.violations:
        return []
    ranked = sorted(response.violations, key=violation_severity_key)
    # One re-bench per order: duplicate claims on one order_id must not crowd
    # the budget out of a legitimate multi-order dissent (keep the most severe
    # claim per order — first after the sort).
    seen: set[str] = set()
    deduped = []
    for v in ranked:
        if v.order_id in seen:
            continue
        seen.add(v.order_id)
        deduped.append(v)
    return deduped[: max(0, budget)]
