"""Distributed benchmark veto — wire layer (Phase 0, observe-only).

The I/O shell around ``epoch/distributed_veto`` (pure protocol) and the
Phase-0 worker primitive (``benchmark_explicit_orders``): assignment fan-out,
the follower's slice-bench runner, response signing, and the leader's
size-capped idempotent ingestion.

INERT UNTIL ARMED. ``DISTRIBUTED_VETO`` (default OFF) gates whether this node
PARTICIPATES — it is a wire kill switch, not a consensus knob: a disabled or
not-yet-upgraded follower answers 404/409, which the leader records as
terminal-UNSUPPORTED (= abstain), so a mixed fleet degrades to exactly
today's behavior. Nothing in this module is called by the coordinator until
the Phase-0 gate lands (next PR); the endpoints are registered but can only
no-op:

- the follower receiver requires a LEADER-SIGNED assignment (there is no
  leader code that fans out yet);
- the leader receiver only accepts responses for assignments in its registry
  (nothing opens a phase yet).

Trust posture (see epoch/distributed_veto for the full protocol rationale):
- Assignments are leader-signed via the SAME personal-sign canonical-JSON
  scheme as round-lifecycle sync (``_authorize_internal_round_sync`` verifies
  at the route). Everything the follower benches — orders, hashes, pins,
  incumbent digest — comes from the signed payload, and any resolution gap is
  a REFUSED, never a fallback.
- Responses are follower-signed (same scheme); the leader authorizes the
  recovered signer against the champion consensus manager's validator set
  (``_is_authorized_signer`` — on-chain registry when wired, in-memory union
  fallback), NOT the routes-layer leader-locked verifiers, which 401 every
  non-leader signer.
- Ingestion rejects on Content-Length BEFORE parsing the body (the 142MB
  submissions.json event-loop freeze is the incident class), then binds the
  response to its assignment and enforces the protocol caps.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from minotaur_subnet.epoch.distributed_veto import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REFUSED,
    VERDICT_OK,
    VERDICT_VETO,
    SliceAssignment,
    SliceViolation,
    VetoPhaseState,
    VetoResponse,
    assign_slices,
    resolve_phase,  # noqa: F401 — re-exported for the Phase-0 coordinator gate
    validate_response,
    verify_assignment_integrity,
)
from minotaur_subnet.harness.order_sampler import (
    calibration_overlap,
    order_replay_hash,
    partition_follower_slices,
)

logger = logging.getLogger(__name__)

# Hard byte cap on a veto-response request body, enforced from the
# Content-Length header BEFORE the body is read. The protocol caps
# (violations<=64, calibration<=16) keep an honest response well under 64KB;
# 256KB leaves headroom without letting anyone stream megabytes onto the
# event loop.
MAX_VETO_RESPONSE_BYTES: int = 262_144
# A slice assignment is bounded (<=50 order ids + hashes + calibration); 512KB
# is generous headroom. Enforced on the FOLLOWER's receiver before parse.
MAX_VETO_ASSIGNMENT_BYTES: int = 524_288

ASSIGNMENT_PATH = "/v1/solver/round/internal/veto-assignment"
RESPONSE_PATH = "/v1/solver/round/internal/veto-response"

# Fan-out outcome labels (leader side, per peer, per tick).
SEND_ACKED = "acked"
SEND_UNSUPPORTED = "unsupported"  # deterministic reject → terminal abstain
SEND_UNREACHABLE = "unreachable"  # transient → re-send next tick


def own_validator_evm() -> str | None:
    """This node's EVM address from the champion peer network's signing key —
    the identity a leader-signed assignment must be addressed to. None (no key
    wired) leaves the addressed-to-me gate inert; a node with no signing key
    can't produce a signed response anyway."""
    try:
        from eth_account import Account

        from .state import get_champion_peer_network

        pk = getattr(get_champion_peer_network(), "private_key", None)
        return Account.from_key(pk).address.lower() if pk else None
    except Exception:  # noqa: BLE001
        return None


def distributed_veto_enabled() -> bool:
    """Wire kill switch (default OFF) — participation, not consensus.

    Deliberately NOT a consensus-relevant constant: it never changes what any
    benchmark computes, only whether this node takes part in the veto phase.
    A node with it off answers 409; the leader maps that to
    terminal-UNSUPPORTED = abstain, which is today's behavior exactly.
    """
    return (os.environ.get("DISTRIBUTED_VETO", "0").strip().lower()) in (
        "1", "true", "yes", "on",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Leader: phase registry (in-memory; the Phase-0 coordinator PR binds it to
# RoundState persistence — this bounded map is the live working set either way)
# ─────────────────────────────────────────────────────────────────────────────

class VetoPhaseRegistry:
    """Bounded in-memory map of round_id → VetoPhaseState (leader side).

    Single-event-loop discipline (like the champion approval collector): all
    mutation happens from coordinator ticks and request handlers on the same
    loop, so a plain dict is race-free. Bounded to the last few rounds so a
    forgotten phase can never grow the process (rounds reopen every ~5min).
    """

    _MAX_ROUNDS = 8

    def __init__(self) -> None:
        self._phases: dict[str, VetoPhaseState] = {}

    def open_phase(self, round_id: str, phase: VetoPhaseState) -> None:
        self._phases[round_id] = phase
        while len(self._phases) > self._MAX_ROUNDS:
            oldest = next(iter(self._phases))
            self._phases.pop(oldest, None)
            logger.info("[distributed-veto] evicted phase for %s (bound)", oldest)

    def get(self, round_id: str) -> VetoPhaseState | None:
        return self._phases.get(round_id)

    def clear(self) -> None:
        self._phases.clear()

    def mark_unsupported(self, round_id: str, validator_evm: str) -> None:
        phase = self._phases.get(round_id)
        if phase is None:
            return
        evm = validator_evm.lower()
        if evm not in phase.unsupported:
            phase.unsupported.append(evm)

    def record_response(
        self, round_id: str, response: VetoResponse,
    ) -> tuple[bool, str]:
        """Idempotent, assignment-bound response ingestion.

        First terminal response per (assignment_id, validator) wins; an exact
        re-send re-ACKs (``duplicate``). A response for a DIFFERENT
        assignment_id than the validator's current assignment is stale
        (previous candidate) and rejected — it must never read as a fresh
        verdict (see ``resolve_phase``'s assignment-bound terminality).
        """
        phase = self._phases.get(round_id)
        if phase is None:
            return False, "unknown round"
        evm = response.validator_evm.lower()
        assignment = next(
            (a for a in phase.assignments if a.validator_evm.lower() == evm),
            None,
        )
        if assignment is None:
            return False, "no assignment for validator"
        if response.assignment_id != assignment.assignment_id:
            return False, "stale assignment"
        ok, reason = validate_response(response, assignment)
        if not ok:
            return False, reason
        existing = phase.responses.get(evm)
        if existing is not None:
            if existing.assignment_id == response.assignment_id:
                return True, "duplicate"
            # current assignment changed since the old response — replace
        phase.responses[evm] = response
        return True, "accepted"


REGISTRY = VetoPhaseRegistry()

# (round_id, validator_evm) -> (assignment_id, signed_payload). The GET
# pull-fallback serves cached signed bytes so an attacker cannot force repeated
# leader-key ECDSA signs on the event loop; re-signed only when the round's
# assignment content changes (new assignment_id).
_SIGNED_SERVE_CACHE: dict[tuple[str, str], tuple[str, dict]] = {}


def _reset_serve_cache() -> None:
    _SIGNED_SERVE_CACHE.clear()


def serve_signed_assignment(
    round_id: str, validator: str, sign_payload: Callable[[dict], dict],
) -> dict | None:
    """Return the leader-signed assignment payload for (round, validator), or
    None if there is no such assignment. Signs at most ONCE per assignment_id."""
    phase = REGISTRY.get(round_id)
    if phase is None:
        return None
    evm = validator.lower()
    assignment = next(
        (a for a in phase.assignments if a.validator_evm.lower() == evm), None,
    )
    if assignment is None:
        return None
    key = (round_id, evm)
    cached = _SIGNED_SERVE_CACHE.get(key)
    if cached is not None and cached[0] == assignment.assignment_id:
        return cached[1]
    signed = sign_payload(assignment.to_payload())
    _SIGNED_SERVE_CACHE[key] = (assignment.assignment_id, signed)
    return signed


# ─────────────────────────────────────────────────────────────────────────────
# Leader: assignment building + fan-out
# ─────────────────────────────────────────────────────────────────────────────

def build_assignments(
    app_store: Any,
    *,
    round_id: str,
    candidate_submission_id: str,
    candidate_image_id: str,
    incumbent_image_id: str,
    fork_pins: dict[Any, int],
    deadline_epoch: int,
    validator_evms: list[str],
    entropy: str,
    leader_api_url: str = "",
    chain_ids: list[int] | None = None,
    records: list[dict[str, Any]] | None = None,
) -> list[SliceAssignment]:
    """Build one signed-payload-ready assignment per reachable validator.

    ``entropy`` MUST be post-close (a close-block hash — see
    ``assign_slices``). ``fork_pins`` is the round's per-chain pin map;
    ``chain_ids`` defaults to the round-anchored chains. ``records`` should be
    the SAME corpus snapshot the round's pack hash was sealed from when the
    caller has one (see ``partition_follower_slices``).

    Both image ids must be bare GHCR digests — the leader builds full pullable
    refs from ITS OWN candidate_repo() and signs them into the assignment, so a
    legacy id would REFUSED-out every slice; the caller (coordinator gate)
    skips fan-out entirely in that case, mirroring the quorum>1 digest gate.
    """
    if chain_ids is None:
        from minotaur_subnet.consensus.round_anchor import ROUND_ANCHOR_CHAINS
        chain_ids = list(ROUND_ANCHOR_CHAINS)

    from minotaur_subnet.harness.image_transport import (
        candidate_repo,
        is_bare_digest,
        make_digest_ref,
    )
    if not is_bare_digest(candidate_image_id) or not is_bare_digest(incumbent_image_id):
        return []  # non-digest images can't be pulled cross-host — skip the phase
    repo = candidate_repo()
    candidate_ref = make_digest_ref(repo, candidate_image_id) or ""
    incumbent_ref = make_digest_ref(repo, incumbent_image_id) or ""
    if not candidate_ref or not incumbent_ref:
        return []

    slices = partition_follower_slices(
        app_store, round_id, chain_ids=chain_ids, records=records,
    )
    if not slices:
        return []
    calibration = calibration_overlap(
        app_store, round_id, chain_ids=chain_ids, records=records,
    )
    mapping = assign_slices(validator_evms, len(slices), entropy)

    pins = {str(k): int(v) for k, v in (fork_pins or {}).items()}
    assignments: list[SliceAssignment] = []
    for evm, idx in sorted(mapping.items(), key=lambda kv: kv[1]):
        orders = slices[idx]
        hashes = {
            str(o.get("order_id")): order_replay_hash(o)
            for o in list(orders) + list(calibration)
        }
        assignments.append(SliceAssignment(
            round_id=round_id,
            slice_index=idx,
            validator_evm=evm,
            candidate_submission_id=candidate_submission_id,
            candidate_image_id=candidate_image_id,
            incumbent_image_id=incumbent_image_id,
            candidate_image_ref=candidate_ref,
            incumbent_image_ref=incumbent_ref,
            order_ids=[str(o.get("order_id")) for o in orders],
            order_hashes=hashes,
            calibration_order_ids=[
                str(o.get("order_id")) for o in calibration
            ],
            fork_pins=pins,
            deadline_epoch=deadline_epoch,
            leader_api_url=leader_api_url,
        ))
    return assignments


async def fan_out_assignments(
    assignments: list[SliceAssignment],
    *,
    peer_urls: dict[str, str],  # validator_evm(lower) -> api base url
    sign_payload: Callable[[dict], dict],
    post_json: Callable[..., Awaitable[tuple[int, Any]]] | None = None,
) -> dict[str, str]:
    """Send each validator ITS assignment. Single-shot, skip-on-fail — the
    coordinator re-calls every tick until response-or-deadline (there is no
    follower-side pull trigger; the re-send loop IS the delivery guarantee).

    Returns {validator_evm: SEND_*}. A single deterministic HTTP reject
    (endpoint missing on :stable, feature disabled, auth reject) maps to
    SEND_UNSUPPORTED for THIS tick — but the coordinator (PR 3) must only mark
    a validator terminal-UNSUPPORTED after K CONSECUTIVE such rejects
    (LOCKED DECISION 12): a one-off 404 mid-watchtower-recreate or an
    epoch-skewed 'deadline elapsed' 422 is transient, and marking it terminal
    on the first reject would drop a capable follower's slice. Network
    failures map to UNREACHABLE (always retry next tick). PR 3 must ALSO pass
    an exclude set of already-responded validators so a completed slice is
    never re-sent (which would re-spawn a duplicate bench once the follower's
    task idempotency window has rolled over).
    """
    poster = post_json or _post_json
    results: dict[str, str] = {}
    for assignment in assignments:
        evm = assignment.validator_evm.lower()
        base = (peer_urls.get(evm) or "").rstrip("/")
        if not base:
            results[evm] = SEND_UNREACHABLE
            continue
        payload = sign_payload(assignment.to_payload())
        try:
            status, _body = await poster(f"{base}{ASSIGNMENT_PATH}", payload)
        except Exception as exc:  # noqa: BLE001 — skip-on-fail transport
            logger.info(
                "[distributed-veto] assignment send to %s failed: %s", evm, exc,
            )
            results[evm] = SEND_UNREACHABLE
            continue
        if status in (200, 202):
            results[evm] = SEND_ACKED
        elif status in (401, 403, 404, 405, 409, 410, 422):
            results[evm] = SEND_UNSUPPORTED
        else:
            results[evm] = SEND_UNREACHABLE
    return results


async def _post_json(
    url: str, payload: dict, timeout_s: float = 20.0,
) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001 — body shape is advisory
                body = None
            return resp.status, body


# ─────────────────────────────────────────────────────────────────────────────
# Follower: accept + supersession + the slice-bench runner
# ─────────────────────────────────────────────────────────────────────────────

# round_id -> (assignment_id, task). At most ONE active veto task per round;
# a newer assignment (different candidate ⇒ different assignment_id) for the
# same round supersedes — the stale bench would hold the shared sim lock for
# dead work and its response would be rejected as stale anyway.
_ACTIVE_SLICE_TASKS: dict[str, tuple[str, asyncio.Task]] = {}


def _reset_active_tasks() -> None:
    """Test hook."""
    _ACTIVE_SLICE_TASKS.clear()


def accept_assignment(
    payload: dict[str, Any],
    *,
    current_epoch: int,
    own_evm: str | None,
    runner_factory: Callable[[SliceAssignment], Awaitable[None]],
    spawn: Callable[[Any], asyncio.Task] = asyncio.create_task,
) -> dict[str, Any]:
    """Follower-side accept: addressed-to-me → integrity → deadline →
    dedupe/supersede → spawn.

    Returns the ACK body for the (already leader-auth'd) POST. The bench runs
    ASYNC — never inline in the handler (the proposal endpoint's inline bench
    is why the harvest needs 300s/peer timeouts; the leader's fan-out must see
    an immediate ACK). All verdicts, including REFUSED, travel via the
    response POST-back, keeping one response channel.

    ``own_evm`` gate is load-bearing: assignment_id excludes the validator, so
    a FOREIGN validator's assignment (fetched from the public GET) has a
    different assignment_id and would otherwise be treated as a new assignment
    for the round — cancelling THIS node's own in-flight legit bench and tying
    up the shared sim lock on someone else's slice. Reject before any
    supersession.
    """
    ok, reason = verify_assignment_integrity(payload)
    if not ok:
        return {"accepted": False, "reason": reason}
    assignment = SliceAssignment.from_payload(payload)
    if own_evm and assignment.validator_evm.lower() != own_evm.lower():
        return {"accepted": False, "reason": "not addressed to me"}
    if current_epoch > assignment.deadline_epoch:
        return {"accepted": False, "reason": "deadline elapsed"}

    active = _ACTIVE_SLICE_TASKS.get(assignment.round_id)
    if active is not None:
        active_id, task = active
        if active_id == assignment.assignment_id:
            return {"accepted": True, "duplicate": True}
        if not task.done():
            task.cancel()
            logger.info(
                "[distributed-veto] superseding slice task for %s "
                "(%s -> %s)", assignment.round_id, active_id,
                assignment.assignment_id,
            )

    task = spawn(runner_factory(assignment))
    _ACTIVE_SLICE_TASKS[assignment.round_id] = (assignment.assignment_id, task)
    _reap_finished_slice_tasks()
    return {"accepted": True}


# Comfortably exceeds max-bench(~30min) / round-cadence(~5min) so an honest but
# slow in-flight bench is never evicted by newer rounds (evicting = a dropped
# veto signal once armed). We only drop DONE tasks; a running task stays until
# it finishes, is superseded by a NEW candidate for its own round, or its
# deadline passes.
_MAX_ACTIVE_SLICE_ROUNDS = 12


def _reap_finished_slice_tasks() -> None:
    for rid in [r for r, (_, t) in _ACTIVE_SLICE_TASKS.items() if t.done()]:
        _ACTIVE_SLICE_TASKS.pop(rid, None)
    # Hard backstop: if still over the bound (a pathological pile-up of running
    # tasks), drop the OLDEST-inserted done-or-not — but log it as anomalous.
    while len(_ACTIVE_SLICE_TASKS) > _MAX_ACTIVE_SLICE_ROUNDS:
        oldest = next(iter(_ACTIVE_SLICE_TASKS))
        _, old_task = _ACTIVE_SLICE_TASKS.pop(oldest)
        if not old_task.done():
            logger.warning(
                "[distributed-veto] evicting a RUNNING slice task for %s "
                "(active rounds > %d) — its verdict will be lost",
                oldest, _MAX_ACTIVE_SLICE_ROUNDS,
            )
            old_task.cancel()


def _refused(assignment: SliceAssignment, reason: str) -> VetoResponse:
    return VetoResponse(
        assignment_id=assignment.assignment_id,
        round_id=assignment.round_id,
        validator_evm=assignment.validator_evm,
        status=STATUS_REFUSED,
        error=reason,
    )


def _failed(assignment: SliceAssignment, reason: str) -> VetoResponse:
    return VetoResponse(
        assignment_id=assignment.assignment_id,
        round_id=assignment.round_id,
        validator_evm=assignment.validator_evm,
        status=STATUS_FAILED,
        error=reason,
    )


def _is_catastrophic(champ: str | None, chal: str | None) -> bool:
    """Exact-integer >FLOOR_BPS cut check — the same cross-multiplied form the
    relative rule uses (no floats)."""
    from minotaur_subnet.epoch.relative_scoring import FLOOR_BPS
    try:
        champ_i = int(champ)  # type: ignore[arg-type]
        chal_i = int(chal)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if champ_i <= 0:
        return False
    return chal_i * 10_000 < champ_i * (10_000 - FLOOR_BPS)


async def run_slice_bench(
    assignment: SliceAssignment,
    *,
    order_lookup: Callable[[str], dict[str, Any] | None],
    worker_factory: Callable[[], Any],
    pull_image: Callable[[str], Awaitable[bool]],
) -> VetoResponse:
    """Bench champion + candidate on the assigned slice; return the verdict.

    Everything is resolved FROM THE SIGNED ASSIGNMENT — orders (content-hash
    validated against the local store), incumbent digest (never the local
    champion record: a lagging follower would bench the wrong champion), fork
    pins (a missing pin is REFUSED, never the live-head fallback the reactive
    path takes). Every gap is a terminal REFUSED/FAILED response — abstain
    semantics, never an exception into the caller.
    """
    from minotaur_subnet.harness.image_transport import is_digest_ref

    # 1. Rebuild the exact order set from the local store, hash-verified.
    slice_orders: list[dict[str, Any]] = []
    calib_orders: list[dict[str, Any]] = []
    for oid in list(assignment.order_ids) + list(assignment.calibration_order_ids):
        record = order_lookup(oid)
        if record is None:
            return _refused(assignment, f"corpus_missing:{oid}")
        if order_replay_hash(record) != assignment.order_hashes.get(oid):
            return _refused(assignment, f"corpus_mismatch:{oid}")
        if oid in assignment.calibration_order_ids:
            calib_orders.append(record)
        else:
            slice_orders.append(record)

    # 2. Pins: single-chain slices only (the harness has ONE scalar
    #    fork_block), and that chain's pin MUST be in the assignment.
    chains = {o.get("chain_id") for o in slice_orders + calib_orders}
    if len(chains) != 1:
        return _refused(assignment, f"multi_chain_slice:{sorted(map(str, chains))}")
    chain = next(iter(chains))
    pin = assignment.fork_pins.get(str(chain))
    if not pin:
        return _refused(assignment, f"no_pin:{chain}")

    # 3. Both images by the LEADER-SIGNED pullable ref (<repo>@sha256:D) — the
    #    repo travels with the digest, so a follower never rebuilds against its
    #    own CANDIDATE_IMAGE_REPO env (which would 404 whenever it differs).
    candidate_ref = assignment.candidate_image_ref
    incumbent_ref = assignment.incumbent_image_ref
    if not is_digest_ref(candidate_ref):
        return _refused(assignment, "candidate_not_digest_ref")
    if not is_digest_ref(incumbent_ref):
        return _refused(assignment, "incumbent_not_digest_ref")
    for label, ref in (("candidate", candidate_ref), ("incumbent", incumbent_ref)):
        if not await pull_image(ref):
            return _failed(assignment, f"pull_failed:{label}")

    # 4. Bench both sides at the ASSIGNMENT's pin.
    from minotaur_subnet.harness.benchmark_worker import ExplicitOrderUnavailable
    from minotaur_subnet.harness.orchestrator import RealSimulationUnavailable

    bench_orders = slice_orders + calib_orders
    try:
        worker = worker_factory()
        worker._epoch_block_number = int(pin)
        champ_results = await worker.benchmark_explicit_orders(
            incumbent_ref, bench_orders,
        )
        chal_results = await worker.benchmark_explicit_orders(
            candidate_ref, bench_orders,
        )
    except ExplicitOrderUnavailable as exc:
        return _refused(assignment, f"{exc.reason} (order {exc.order_id})")
    except RealSimulationUnavailable:
        return _failed(assignment, "no_real_sim")
    except asyncio.CancelledError:
        raise  # supersession — never swallow cancellation
    except Exception as exc:  # noqa: BLE001 — any bench error is an abstain
        return _failed(assignment, f"bench_error:{exc}")

    # 5. Slice-local verdict with the AUTHORITATIVE rule, then extract the
    #    hard-veto evidence. Calibration rows report both sides' outputs but
    #    NEVER contribute violations (validate_response enforces it too).
    from minotaur_subnet.epoch.relative_scoring import evaluate_relative_adoption

    verdict = evaluate_relative_adoption(champ_results, chal_results)

    # The harness labels every row f"{app_id}:{scenario_name}" =
    # "{app_id}:hist:{order_id}". Map rows back to order_id via the RESOLVED
    # records (their app_id + order_id), never by string-stripping — a shape
    # drift must fail the coverage assert below, not silently skip every row
    # into a vacuous OK.
    label_to_oid: dict[str, str] = {}
    slice_ids: set[str] = set()
    calib_ids: set[str] = set()
    for o in slice_orders:
        oid = str(o.get("order_id"))
        label_to_oid[f"{o.get('app_id')}:hist:{oid}"] = oid
        slice_ids.add(oid)
    for o in calib_orders:
        oid = str(o.get("order_id"))
        label_to_oid[f"{o.get('app_id')}:hist:{oid}"] = oid
        calib_ids.add(oid)

    # A challenger side that produced NOTHING via a HARNESS failure (timeout,
    # respawn exhaustion, run-budget tail) rather than a real no-plan — the row
    # carries error!=None + raw_output None. That is infra noise, not a dropped
    # order: bucket it into counts['bench_error'] so it never becomes a
    # hard-veto claim that burns the leader's re-verify budget and strikes an
    # honest-but-slow follower.
    chal_errored = {
        str(getattr(r, "intent_id", "")): bool(getattr(r, "error", None))
        for r in chal_results
    }

    violations: list[SliceViolation] = []
    counts = {
        "wins": 0, "regressions": 0, "catastrophic": 0, "dropped": 0,
        "blind_spot_covers": 0, "matched": 0, "compared": 0, "bench_error": 0,
    }
    calibration_rows: list[dict[str, str]] = []
    matched_rows = 0
    for row in verdict.get("per_order", []):
        iid = str(row.get("intent_id", ""))
        oid = label_to_oid.get(iid)
        if oid is None:
            continue  # synthetic/foreign rows can never enter evidence
        matched_rows += 1
        champ_raw = row.get("champ")
        chal_raw = row.get("chal")
        if oid in calib_ids:
            calibration_rows.append({
                "order_id": oid,
                "champ_raw": "" if champ_raw is None else str(champ_raw),
                "chal_raw": "" if chal_raw is None else str(chal_raw),
            })
            continue
        v = row.get("verdict")
        if v != "skip":
            counts["compared"] += 1
        if v == "win":
            counts["wins"] += 1
        elif v == "blind_spot_cover":
            counts["blind_spot_covers"] += 1
        elif v == "matched":
            counts["matched"] += 1
        elif v == "dropped":
            if chal_errored.get(iid):
                counts["bench_error"] += 1  # infra noise, not a real drop
                continue
            counts["dropped"] += 1
            violations.append(SliceViolation(
                order_id=oid, kind="dropped",
                champ_raw="" if champ_raw is None else str(champ_raw),
                chal_raw="" if chal_raw is None else str(chal_raw),
            ))
        elif v == "regression":
            counts["regressions"] += 1
            if _is_catastrophic(champ_raw, chal_raw):
                counts["catastrophic"] += 1
                violations.append(SliceViolation(
                    order_id=oid, kind="catastrophic",
                    champ_raw=str(champ_raw), chal_raw=str(chal_raw),
                ))

    # Coverage assert: every slice + calibration order MUST have produced a
    # matched row. A shortfall means label drift or a lost row — REFUSE loudly
    # rather than emit a partial/vacuous verdict (the whole point of the strict
    # explicit-order path).
    expected = len(slice_ids) + len(calib_ids)
    if matched_rows != expected:
        return _refused(assignment, f"row_coverage:{matched_rows}/{expected}")

    return VetoResponse(
        assignment_id=assignment.assignment_id,
        round_id=assignment.round_id,
        validator_evm=assignment.validator_evm,
        status=STATUS_COMPLETED,
        verdict=VERDICT_VETO if violations else VERDICT_OK,
        violations=violations,
        counts=counts,
        calibration=calibration_rows,
    )


async def submit_response(
    response: VetoResponse,
    *,
    leader_api_url: str,
    private_key: str | None,
    deadline_epoch: int,
    current_epoch_fn: Callable[[], int],
    post_json: Callable[..., Awaitable[tuple[int, Any]]] | None = None,
    retry_delay_s: float = 15.0,
    jitter: Callable[[], float] = lambda: 0.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_transient_retries: int = 8,
) -> bool:
    """POST the signed response to the leader, retrying until accepted,
    terminally rejected, the transient-retry budget is spent, or the deadline
    passes (late = the leader already counted us as an abstain — stop burning
    the connection).

    Transient failures (network error, 503, 5xx) back off with jitter and are
    capped at ``max_transient_retries`` — a persistent condition (e.g. 503
    'manager not wired', which cannot resolve within the run) must NOT loop
    every 15s for the whole window, and the jitter de-synchronizes the N
    followers that finish benching in the same tick."""
    base = (leader_api_url or "").rstrip("/")
    if not base:
        logger.warning("[distributed-veto] no leader_api_url — response dropped")
        return False
    if not private_key:
        logger.warning("[distributed-veto] no signing key — response dropped")
        return False
    payload = sign_response_payload(response.to_payload(), private_key)
    poster = post_json or _post_json
    url = f"{base}{RESPONSE_PATH}"
    transient = 0

    async def _backoff() -> bool:
        nonlocal transient
        transient += 1
        if transient > max_transient_retries:
            logger.info(
                "[distributed-veto] response transient-retry budget spent — dropped",
            )
            return False
        await sleep(retry_delay_s + jitter())
        return True

    while current_epoch_fn() <= deadline_epoch:
        try:
            status, body = await poster(url, payload)
        except Exception as exc:  # noqa: BLE001 — transient transport
            logger.info("[distributed-veto] response POST failed: %s", exc)
            if not await _backoff():
                return False
            continue
        if status == 200:
            return True
        if status in (400, 401, 404, 409, 413, 422):
            # Deterministic reject (stale assignment, unknown round, auth) —
            # retrying the identical payload cannot succeed.
            logger.info(
                "[distributed-veto] response rejected (%s): %s", status, body,
            )
            return False
        if not await _backoff():  # 5xx / 503 — bounded
            return False
    logger.info("[distributed-veto] response deadline elapsed — dropped")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Response signing (follower) / verification (leader)
# ─────────────────────────────────────────────────────────────────────────────

def sign_response_payload(payload: dict[str, Any], private_key: str) -> dict[str, Any]:
    """Personal-sign the canonical JSON of the response payload.

    Same scheme as round-lifecycle sync (`_sign_internal_round_payload`):
    ``validator_evm`` is FORCED to the signing key's address (a claimed evm
    that differs from the signer is meaningless), then the canonical JSON of
    everything except ``validator_signature`` is signed.
    """
    import json as _json

    from eth_account import Account
    from eth_account.messages import encode_defunct

    signed = dict(payload)
    signed.pop("validator_signature", None)
    signed["validator_evm"] = Account.from_key(private_key).address.lower()
    canonical = _json.dumps(signed, sort_keys=True, separators=(",", ":"))
    sig = Account.sign_message(
        encode_defunct(text=canonical), private_key=private_key,
    )
    signed["validator_signature"] = sig.signature.hex()
    return signed


def verify_response_signature(raw: dict[str, Any]) -> tuple[str | None, str]:
    """Recover the signer of a response payload. Returns (evm_lower, "") or
    (None, reason). AUTHORIZATION (is this a validator?) is the caller's job —
    this only proves the payload binds to the claimed key."""
    sig_hex = str(raw.get("validator_signature", "") or "").strip()
    claimed = str(raw.get("validator_evm", "") or "").strip()
    if not sig_hex or not claimed:
        return None, "missing validator_evm / validator_signature"
    import json as _json

    from eth_account import Account
    from eth_account.messages import encode_defunct

    payload = dict(raw)
    payload.pop("validator_signature", None)
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    try:
        recovered = Account.recover_message(
            encode_defunct(text=canonical), signature=sig_hex,
        )
    except Exception as exc:  # noqa: BLE001 — malformed sig is a 401, not a 500
        return None, f"signature recovery failed: {exc}"
    if recovered.lower() != claimed.lower():
        return None, "signer != claimed validator_evm"
    return recovered.lower(), ""


def ingest_response(
    raw: dict[str, Any],
    *,
    registry: VetoPhaseRegistry,
    is_authorized_signer: Callable[[str], bool],
) -> tuple[int, dict[str, Any]]:
    """Leader-side ingestion (post-Content-Length-cap): signature →
    authorization → assignment binding → idempotent record. Returns
    (http_status, body). Every reject is deterministic so the follower's
    retry loop knows to stop."""
    evm, err = verify_response_signature(raw)
    if evm is None:
        return 401, {"accepted": False, "reason": err}
    if not is_authorized_signer(evm):
        return 401, {"accepted": False, "reason": "signer not an authorized validator"}
    try:
        response = VetoResponse.from_payload(raw)
    except (TypeError, ValueError) as exc:
        return 422, {"accepted": False, "reason": f"malformed response: {exc}"}
    ok, reason = registry.record_response(response.round_id, response)
    if not ok:
        status = 404 if reason in ("unknown round", "no assignment for validator") else 409
        return status, {"accepted": False, "reason": reason}
    return 200, {"accepted": True, "detail": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Production glue (lazy ctx imports — routes call these)
# ─────────────────────────────────────────────────────────────────────────────

def _production_worker_factory() -> Any:
    """Fresh BenchmarkWorker mirroring the reactive-verify construction
    (fresh instance per bench; the shared anvil ``_sim_lock`` is the real
    serializer). ``benchmark_explicit_orders`` itself REFUSES when the
    simulator is missing, regardless of the require-real-sim env."""
    from minotaur_subnet.api.routes import apps as _apps_module
    from minotaur_subnet.api.server_context import ctx
    from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker
    from minotaur_subnet.harness.orchestrator import require_real_sim_default

    from .state import get_store

    return BenchmarkWorker(
        submission_store=get_store(),
        app_store=ctx.store,
        use_docker=True,
        simulator=getattr(_apps_module, "_simulator", None),
        require_real_sim=require_real_sim_default(),
    )


def _production_order_lookup(order_id: str) -> dict[str, Any] | None:
    from minotaur_subnet.api.server_context import ctx

    store = getattr(ctx, "store", None)
    if store is None:
        return None
    try:
        return store.get_order(order_id)
    except Exception:  # noqa: BLE001 — lookup failure = corpus_missing
        return None


async def _production_runner(assignment: SliceAssignment) -> None:
    """The spawned follower task: bench the slice, then POST the verdict back
    with retry-until-deadline. Terminal by construction — every failure mode
    becomes a REFUSED/FAILED response or a logged drop, never an unhandled
    task exception."""
    from .round_manager import _current_solver_round_epoch

    try:
        from .champion_consensus import _pull_image_by_digest

        response = await run_slice_bench(
            assignment,
            order_lookup=_production_order_lookup,
            worker_factory=_production_worker_factory,
            pull_image=_pull_image_by_digest,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — belt over run_slice_bench's braces
        logger.exception("[distributed-veto] slice runner crashed: %s", exc)
        response = _failed(assignment, f"runner_error:{exc}")

    try:
        private_key = None
        try:
            from .state import get_champion_peer_network

            network = get_champion_peer_network()
            private_key = getattr(network, "private_key", None)
        except Exception:  # noqa: BLE001
            private_key = None

        import random

        await submit_response(
            response,
            leader_api_url=assignment.leader_api_url,
            private_key=private_key,
            deadline_epoch=assignment.deadline_epoch,
            current_epoch_fn=_current_solver_round_epoch,
            jitter=lambda: random.uniform(0.0, 5.0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — signing/transport must not escape the task
        logger.warning(
            "[distributed-veto] response submission for %s failed: %s",
            assignment.round_id, exc,
        )
