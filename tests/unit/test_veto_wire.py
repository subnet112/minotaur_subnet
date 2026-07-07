"""Distributed-veto wire layer (veto_wire — Phase 0, observe-only).

Pins: registry idempotency + assignment binding, per-peer distinct-payload
fan-out with UNSUPPORTED/UNREACHABLE classification, follower accept +
supersession, the slice-bench runner's REFUSED/FAILED/verdict paths through
the REAL relative rule, response signing round-trips, and ingestion ordering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from minotaur_subnet.api.routes.submissions import veto_wire
from minotaur_subnet.api.routes.submissions.veto_wire import (
    SEND_ACKED,
    SEND_UNREACHABLE,
    SEND_UNSUPPORTED,
    VetoPhaseRegistry,
    accept_assignment,
    build_assignments,
    fan_out_assignments,
    ingest_response,
    run_slice_bench,
    sign_response_payload,
    submit_response,
    verify_response_signature,
)
from minotaur_subnet.epoch.distributed_veto import (
    STATUS_COMPLETED,
    STATUS_REFUSED,
    VERDICT_OK,
    VERDICT_VETO,
    SliceAssignment,
    VetoPhaseState,
    VetoResponse,
)
from minotaur_subnet.harness.order_sampler import order_replay_hash

# anvil dev key #0 — the same throwaway key other unit tests use
_PRIV = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_PRIV_EVM = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _order(order_id: str, chain_id: int = 8453) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "app_id": "app_dex",
        "chain_id": chain_id,
        "status": "filled",
        "intent_function": "swap",
        "params": {
            "input_token": "0xWETH",
            "output_token": f"0xOUT_{order_id}",
            "input_amount": "1000000000000000000",
        },
    }


def _assignment(
    order_ids=("ord_a", "ord_b", "ord_c"),
    calib_ids=("cal_1",),
    validator: str = "0xv1",
    deadline: int = 100,
    pins: dict[str, int] | None = None,
) -> tuple[SliceAssignment, dict[str, dict]]:
    orders = {oid: _order(oid) for oid in list(order_ids) + list(calib_ids)}
    assignment = SliceAssignment(
        round_id="round-e9-n1",
        slice_index=0,
        validator_evm=validator,
        candidate_submission_id="sub_1",
        candidate_image_id=_DIGEST_A,
        incumbent_image_id=_DIGEST_B,
        candidate_image_ref=f"ghcr.io/x@sha256:{_DIGEST_A}",
        incumbent_image_ref=f"ghcr.io/x@sha256:{_DIGEST_B}",
        order_ids=list(order_ids),
        order_hashes={oid: order_replay_hash(o) for oid, o in orders.items()},
        calibration_order_ids=list(calib_ids),
        fork_pins=pins if pins is not None else {"8453": 28000000},
        deadline_epoch=deadline,
        leader_api_url="http://leader:8080",
    )
    return assignment, orders


def _completed(assignment: SliceAssignment, verdict=VERDICT_OK, violations=None):
    return VetoResponse(
        assignment_id=assignment.assignment_id,
        round_id=assignment.round_id,
        validator_evm=assignment.validator_evm,
        status=STATUS_COMPLETED,
        verdict=verdict,
        violations=violations or [],
    )


@pytest.fixture(autouse=True)
def _clean_module_state():
    veto_wire._reset_active_tasks()
    yield
    veto_wire._reset_active_tasks()


# ── registry ─────────────────────────────────────────────────────────────────

class TestRegistry:
    def _phase(self, assignment) -> VetoPhaseState:
        return VetoPhaseState(
            candidate_submission_id="sub_1",
            candidate_image_id=_DIGEST_A,
            deadline_epoch=100,
            assignments=[assignment],
        )

    def test_record_accept_then_duplicate(self):
        reg = VetoPhaseRegistry()
        a, _ = _assignment()
        reg.open_phase(a.round_id, self._phase(a))
        ok, reason = reg.record_response(a.round_id, _completed(a))
        assert ok and reason == "accepted"
        ok, reason = reg.record_response(a.round_id, _completed(a))
        assert ok and reason == "duplicate"
        assert len(reg.get(a.round_id).responses) == 1

    def test_unknown_round_and_validator(self):
        reg = VetoPhaseRegistry()
        a, _ = _assignment()
        assert reg.record_response("round-nope", _completed(a)) == (
            False, "unknown round",
        )
        reg.open_phase(a.round_id, self._phase(a))
        stranger = _completed(a)
        stranger.validator_evm = "0xstranger"
        ok, reason = reg.record_response(a.round_id, stranger)
        assert not ok and reason == "no assignment for validator"

    def test_stale_assignment_rejected(self):
        reg = VetoPhaseRegistry()
        a, _ = _assignment()
        reg.open_phase(a.round_id, self._phase(a))
        stale = _completed(a)
        stale.assignment_id = "0" * 32
        ok, reason = reg.record_response(a.round_id, stale)
        assert not ok and reason == "stale assignment"

    def test_bounded_rounds(self):
        reg = VetoPhaseRegistry()
        for i in range(reg._MAX_ROUNDS + 3):
            a, _ = _assignment()
            a.round_id = f"round-e{i}-n1"
            reg.open_phase(a.round_id, self._phase(a))
        assert reg.get("round-e0-n1") is None
        assert reg.get(f"round-e{reg._MAX_ROUNDS + 2}-n1") is not None

    def test_mark_unsupported(self):
        reg = VetoPhaseRegistry()
        a, _ = _assignment()
        reg.open_phase(a.round_id, self._phase(a))
        reg.mark_unsupported(a.round_id, "0xV1")
        reg.mark_unsupported(a.round_id, "0xv1")
        assert reg.get(a.round_id).unsupported == ["0xv1"]


# ── leader: build + fan-out ──────────────────────────────────────────────────

class _FakeAppStore:
    def __init__(self, orders):
        self._orders = orders

    def list_orders(self):
        return list(self._orders)


class TestBuildAssignments:
    def test_builds_one_per_mapped_validator(self):
        corpus = [_order(f"ord_{i:04d}") for i in range(160)]
        assignments = build_assignments(
            _FakeAppStore(corpus),
            round_id="round-e9-n1",
            candidate_submission_id="sub_1",
            candidate_image_id=_DIGEST_A,
            incumbent_image_id=_DIGEST_B,
            fork_pins={8453: 28000000},
            deadline_epoch=77,
            validator_evms=["0xV1", "0xV2", "0xV3", "0xV4"],
            entropy="close_block_hash",
            leader_api_url="http://leader:8080",
            chain_ids=[8453],
        )
        # 110 remainder → 3 slices (50/50/10); 4 validators → 3 get work
        assert len(assignments) == 3
        seen_orders: set[str] = set()
        for a in assignments:
            from minotaur_subnet.epoch.distributed_veto import (
                verify_assignment_integrity,
            )
            ok, reason = verify_assignment_integrity(a.to_payload())
            assert ok, reason
            assert a.fork_pins == {"8453": 28000000}
            assert a.deadline_epoch == 77
            assert a.leader_api_url == "http://leader:8080"
            assert a.calibration_order_ids  # overlap present
            assert set(a.order_hashes) == set(a.order_ids) | set(
                a.calibration_order_ids
            )
            assert not (set(a.order_ids) & seen_orders)
            seen_orders |= set(a.order_ids)

    def test_empty_corpus_builds_nothing(self):
        assert build_assignments(
            _FakeAppStore([]),
            round_id="r", candidate_submission_id="s",
            candidate_image_id=_DIGEST_A, incumbent_image_id=_DIGEST_B,
            fork_pins={}, deadline_epoch=1, validator_evms=["0xV1"],
            entropy="e", chain_ids=[8453],
        ) == []


class TestFanOut:
    @pytest.mark.asyncio
    async def test_status_classification_and_distinct_payloads(self):
        a1, _ = _assignment(validator="0xv1")
        a2, _ = _assignment(order_ids=("ord_x",), calib_ids=(), validator="0xv2")
        a3, _ = _assignment(order_ids=("ord_y",), calib_ids=(), validator="0xv3")
        a4, _ = _assignment(order_ids=("ord_z",), calib_ids=(), validator="0xv4")
        sent: dict[str, dict] = {}

        async def post_json(url, payload):
            sent[payload["validator_evm"]] = {"url": url, "payload": payload}
            if payload["validator_evm"] == "0xv2":
                return 409, {"detail": "disabled"}
            if payload["validator_evm"] == "0xv3":
                raise ConnectionError("down")
            return 200, {"accepted": True}

        out = await fan_out_assignments(
            [a1, a2, a3, a4],
            peer_urls={
                "0xv1": "http://v1:8080/", "0xv2": "http://v2:8080",
                "0xv3": "http://v3:8080",  # v4 missing → unreachable
            },
            sign_payload=lambda p: {**p, "proposer": "0xleader"},
            post_json=post_json,
        )
        assert out == {
            "0xv1": SEND_ACKED, "0xv2": SEND_UNSUPPORTED,
            "0xv3": SEND_UNREACHABLE, "0xv4": SEND_UNREACHABLE,
        }
        # per-peer DISTINCT payloads, each signed, correct path
        assert sent["0xv1"]["payload"]["order_ids"] == ["ord_a", "ord_b", "ord_c"]
        assert sent["0xv2"]["payload"]["order_ids"] == ["ord_x"]
        assert sent["0xv1"]["payload"]["proposer"] == "0xleader"
        assert sent["0xv1"]["url"] == "http://v1:8080" + veto_wire.ASSIGNMENT_PATH


# ── follower: accept + supersession ──────────────────────────────────────────

class _FakeTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def _fake_spawn(record: list):
    def spawn(coro):
        coro.close()  # never actually run in accept tests
        task = _FakeTask()
        record.append(task)
        return task
    return spawn


async def _noop_runner(assignment):
    return None


class TestAcceptAssignment:
    def test_accepts_and_spawns(self):
        a, _ = _assignment()
        tasks: list[_FakeTask] = []
        ack = accept_assignment(
            a.to_payload(), current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=_fake_spawn(tasks),
        )
        assert ack == {"accepted": True}
        assert len(tasks) == 1

    def test_integrity_failure_rejected(self):
        a, _ = _assignment()
        payload = a.to_payload()
        payload["order_ids"] = list(reversed(payload["order_ids"]))
        ack = accept_assignment(
            payload, current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=_fake_spawn([]),
        )
        assert not ack["accepted"] and "slice_hash" in ack["reason"]

    def test_deadline_elapsed_rejected(self):
        a, _ = _assignment(deadline=10)
        ack = accept_assignment(
            a.to_payload(), current_epoch=11, own_evm=None,
            runner_factory=_noop_runner, spawn=_fake_spawn([]),
        )
        assert not ack["accepted"] and "deadline" in ack["reason"]

    def test_duplicate_reack_without_respawn(self):
        a, _ = _assignment()
        tasks: list[_FakeTask] = []
        spawn = _fake_spawn(tasks)
        accept_assignment(
            a.to_payload(), current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=spawn,
        )
        ack = accept_assignment(
            a.to_payload(), current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=spawn,
        )
        assert ack == {"accepted": True, "duplicate": True}
        assert len(tasks) == 1

    def test_new_candidate_supersedes(self):
        a, _ = _assignment()
        tasks: list[_FakeTask] = []
        spawn = _fake_spawn(tasks)
        accept_assignment(
            a.to_payload(), current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=spawn,
        )
        b, _ = _assignment()
        b.candidate_image_id = "c" * 64  # new candidate → new assignment_id
        ack = accept_assignment(
            b.to_payload(), current_epoch=5, own_evm=None,
            runner_factory=_noop_runner, spawn=spawn,
        )
        assert ack == {"accepted": True}
        assert len(tasks) == 2
        assert tasks[0].cancelled, "stale slice bench must be cancelled"


# ── follower: the slice-bench runner ─────────────────────────────────────────

def _result(oid: str, raw: str | None, app_id: str = "app_dex", error=None):
    # The harness labels rows f"{app_id}:{scenario_name}" = "{app_id}:hist:{oid}".
    return SimpleNamespace(
        intent_id=f"{app_id}:hist:{oid}", raw_output=raw, error=error,
    )


def _runner_kwargs(orders: dict[str, dict], champ, chal, *, pull_ok=True):
    async def pull_image(ref):
        return pull_ok

    worker = MagicMock()
    worker.benchmark_explicit_orders = AsyncMock(side_effect=[champ, chal])
    return {
        "order_lookup": lambda oid: orders.get(oid),
        "worker_factory": lambda: worker,
        "pull_image": pull_image,
    }, worker


class TestRunSliceBench:
    @pytest.mark.asyncio
    async def test_completed_with_violations_and_calibration(self):
        a, orders = _assignment()
        champ = [
            _result("ord_a", "1000000"),   # challenger cuts 10% → catastrophic
            _result("ord_b", "500000"),    # challenger drops → dropped
            _result("ord_c", "700000"),    # matched
            _result("cal_1", "42"),        # calibration row, NOT evidence
        ]
        chal = [
            _result("ord_a", "900000"),
            _result("ord_b", None),
            _result("ord_c", "700000"),
            _result("cal_1", "40"),
        ]
        kwargs, worker = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)

        assert resp.status == STATUS_COMPLETED
        assert resp.verdict == VERDICT_VETO
        got = {(v.order_id, v.kind) for v in resp.violations}
        assert got == {("ord_a", "catastrophic"), ("ord_b", "dropped")}
        by_id = {v.order_id: v for v in resp.violations}
        assert by_id["ord_a"].champ_raw == "1000000"
        assert by_id["ord_a"].chal_raw == "900000"
        assert resp.counts["catastrophic"] == 1
        assert resp.counts["dropped"] == 1
        assert resp.counts["matched"] == 1
        assert resp.calibration == [
            {"order_id": "cal_1", "champ_raw": "42", "chal_raw": "40"},
        ]
        # pins from the ASSIGNMENT, applied before benching
        assert worker._epoch_block_number == 28000000
        # both benches got slice + calibration orders
        first_call = worker.benchmark_explicit_orders.await_args_list[0]
        assert [o["order_id"] for o in first_call.args[1]] == [
            "ord_a", "ord_b", "ord_c", "cal_1",
        ]
        # incumbent benched FROM THE ASSIGNMENT digest, not a local record
        assert _DIGEST_B in first_call.args[0]

    @pytest.mark.asyncio
    async def test_ok_when_no_hard_violations(self):
        a, orders = _assignment(order_ids=("ord_a",), calib_ids=())
        champ = [_result("ord_a", "1000000")]
        chal = [_result("ord_a", "1000000")]
        kwargs, _ = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_COMPLETED
        assert resp.verdict == VERDICT_OK
        assert resp.violations == []

    @pytest.mark.asyncio
    async def test_small_regression_is_not_a_violation(self):
        # 0.5% cut: a regression under the 1% floor is netting material for
        # the leader's canonical corpus — NEVER slice-veto evidence.
        a, orders = _assignment(order_ids=("ord_a",), calib_ids=())
        champ = [_result("ord_a", "1000000")]
        chal = [_result("ord_a", "995000")]
        kwargs, _ = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.verdict == VERDICT_OK
        assert resp.counts["regressions"] == 1
        assert resp.counts["catastrophic"] == 0

    @pytest.mark.asyncio
    async def test_corpus_missing_and_mismatch_refuse(self):
        a, orders = _assignment()
        kwargs, _ = _runner_kwargs(orders, [], [])
        kwargs["order_lookup"] = lambda oid: None
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_REFUSED
        assert resp.error.startswith("corpus_missing:")

        drifted = dict(orders)
        bad = dict(drifted["ord_a"])
        bad["params"] = dict(bad["params"], input_amount="7")
        drifted["ord_a"] = bad
        kwargs, _ = _runner_kwargs(drifted, [], [])
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_REFUSED
        assert resp.error == "corpus_mismatch:ord_a"

    @pytest.mark.asyncio
    async def test_missing_pin_refuses_never_live_head(self):
        a, orders = _assignment(pins={})
        kwargs, worker = _runner_kwargs(orders, [], [])
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_REFUSED
        assert resp.error.startswith("no_pin:")
        worker.benchmark_explicit_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_digest_ref_refuses(self):
        a, orders = _assignment()
        a.candidate_image_ref = "ghcr.io/x:pr-9"  # a tag, not a @sha256 digest ref
        kwargs, worker = _runner_kwargs(orders, [], [])
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_REFUSED
        assert resp.error == "candidate_not_digest_ref"
        worker.benchmark_explicit_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_row_coverage_shortfall_refuses(self):
        # A missing per-order row (label drift / lost scenario) must REFUSE
        # loudly, never emit a partial verdict — the vacuous-OK guard.
        a, orders = _assignment(order_ids=("ord_a", "ord_b"), calib_ids=())
        champ = [_result("ord_a", "1000000")]  # ord_b row absent
        chal = [_result("ord_a", "1000000")]
        kwargs, _ = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_REFUSED
        assert resp.error == "row_coverage:1/2"

    @pytest.mark.asyncio
    async def test_challenger_bench_error_is_not_a_dropped_violation(self):
        # A HARNESS failure on the challenger (timeout/respawn/run-budget) yields
        # raw_output None + error set — infra noise, NOT a dropped order. It must
        # bucket into counts['bench_error'], never a hard-veto claim that burns
        # the leader's re-verify budget and strikes an honest-but-slow follower.
        a, orders = _assignment(order_ids=("ord_a",), calib_ids=())
        champ = [_result("ord_a", "1000000")]
        chal = [_result("ord_a", None, error="skipped: total run budget exceeded")]
        kwargs, _ = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == STATUS_COMPLETED
        assert resp.verdict == VERDICT_OK
        assert resp.violations == []
        assert resp.counts["bench_error"] == 1
        assert resp.counts["dropped"] == 0

    @pytest.mark.asyncio
    async def test_genuine_no_plan_drop_is_a_violation(self):
        # Contrast: challenger genuinely returned nothing (no error) → real drop.
        a, orders = _assignment(order_ids=("ord_a",), calib_ids=())
        champ = [_result("ord_a", "1000000")]
        chal = [_result("ord_a", None, error=None)]
        kwargs, _ = _runner_kwargs(orders, champ, chal)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.verdict == VERDICT_VETO
        assert {(v.order_id, v.kind) for v in resp.violations} == {("ord_a", "dropped")}

    @pytest.mark.asyncio
    async def test_pull_failure_fails(self):
        a, orders = _assignment()
        kwargs, _ = _runner_kwargs(orders, [], [], pull_ok=False)
        resp = await run_slice_bench(a, **kwargs)
        assert resp.status == "failed"
        assert resp.error == "pull_failed:candidate"

    @pytest.mark.asyncio
    async def test_bench_errors_map_to_terminal_responses(self):
        from minotaur_subnet.harness.benchmark_worker import (
            ExplicitOrderUnavailable,
        )
        from minotaur_subnet.harness.orchestrator import (
            RealSimulationUnavailable,
        )

        a, orders = _assignment()
        for exc, status, marker in (
            (ExplicitOrderUnavailable("ord_b", "missing_app:x"), STATUS_REFUSED,
             "missing_app:x"),
            (RealSimulationUnavailable("no sim"), "failed", "no_real_sim"),
            (RuntimeError("boom"), "failed", "bench_error:boom"),
        ):
            kwargs, worker = _runner_kwargs(orders, [], [])
            worker.benchmark_explicit_orders = AsyncMock(side_effect=exc)
            resp = await run_slice_bench(a, **kwargs)
            assert resp.status == status
            assert marker in resp.error


# ── response signing / ingestion ─────────────────────────────────────────────

class TestResponseSigning:
    def test_round_trip_and_evm_forcing(self):
        a, _ = _assignment(validator="0xWRONG")  # claimed evm ignored
        payload = _completed(a).to_payload()
        signed = sign_response_payload(payload, _PRIV)
        assert signed["validator_evm"] == _PRIV_EVM
        evm, err = verify_response_signature(signed)
        assert err == "" and evm == _PRIV_EVM

    def test_tamper_detected(self):
        a, _ = _assignment()
        signed = sign_response_payload(_completed(a).to_payload(), _PRIV)
        tampered = dict(signed, verdict=VERDICT_VETO)
        evm, err = verify_response_signature(tampered)
        # a tampered payload recovers to a DIFFERENT address than claimed
        assert evm is None and err != ""

    def test_missing_fields(self):
        evm, err = verify_response_signature({"validator_evm": "0xa"})
        assert evm is None and "missing" in err


class TestIngestResponse:
    def _setup(self):
        reg = VetoPhaseRegistry()
        a, _ = _assignment(validator=_PRIV_EVM)
        phase = VetoPhaseState(
            candidate_submission_id="sub_1",
            candidate_image_id=_DIGEST_A,
            deadline_epoch=100,
            assignments=[a],
        )
        reg.open_phase(a.round_id, phase)
        return reg, a

    def test_accept_then_duplicate(self):
        reg, a = self._setup()
        signed = sign_response_payload(_completed(a).to_payload(), _PRIV)
        status, body = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: True,
        )
        assert status == 200 and body["accepted"]
        status, body = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: True,
        )
        assert status == 200 and body["detail"] == "duplicate"

    def test_bad_signature_401(self):
        reg, a = self._setup()
        signed = sign_response_payload(_completed(a).to_payload(), _PRIV)
        signed["verdict"] = VERDICT_VETO  # tamper
        status, _ = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: True,
        )
        assert status == 401

    def test_unauthorized_signer_401(self):
        reg, a = self._setup()
        signed = sign_response_payload(_completed(a).to_payload(), _PRIV)
        status, body = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: False,
        )
        assert status == 401 and "authorized" in body["reason"]

    def test_unknown_round_404_and_stale_409(self):
        reg, a = self._setup()
        other, _ = _assignment(validator=_PRIV_EVM)
        other.round_id = "round-e999-n1"
        signed = sign_response_payload(_completed(other).to_payload(), _PRIV)
        status, _ = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: True,
        )
        assert status == 404

        stale = _completed(a)
        stale.assignment_id = "0" * 32
        signed = sign_response_payload(stale.to_payload(), _PRIV)
        status, body = ingest_response(
            signed, registry=reg, is_authorized_signer=lambda e: True,
        )
        assert status == 409 and body["reason"] == "stale assignment"


# ── follower: response submission retry loop ─────────────────────────────────

class TestSubmitResponse:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        a, _ = _assignment()
        calls = []

        async def post_json(url, payload):
            calls.append((url, payload))
            return 200, {"accepted": True}

        ok = await submit_response(
            _completed(a),
            leader_api_url="http://leader:8080/",
            private_key=_PRIV,
            deadline_epoch=100,
            current_epoch_fn=lambda: 1,
            post_json=post_json,
        )
        assert ok
        url, payload = calls[0]
        assert url == "http://leader:8080" + veto_wire.RESPONSE_PATH
        assert payload["validator_signature"]

    @pytest.mark.asyncio
    async def test_deterministic_reject_stops_retrying(self):
        a, _ = _assignment()
        calls = []

        async def post_json(url, payload):
            calls.append(1)
            return 409, {"reason": "stale assignment"}

        ok = await submit_response(
            _completed(a), leader_api_url="http://leader:8080",
            private_key=_PRIV, deadline_epoch=100,
            current_epoch_fn=lambda: 1, post_json=post_json,
        )
        assert not ok and len(calls) == 1

    @pytest.mark.asyncio
    async def test_transient_retries_until_deadline(self):
        a, _ = _assignment()
        clock = {"epoch": 1}
        attempts = []

        async def post_json(url, payload):
            attempts.append(1)
            raise ConnectionError("down")

        async def sleep(_):
            clock["epoch"] += 40  # two attempts, then past the deadline

        ok = await submit_response(
            _completed(a), leader_api_url="http://leader:8080",
            private_key=_PRIV, deadline_epoch=60,
            current_epoch_fn=lambda: clock["epoch"],
            post_json=post_json, sleep=sleep,
        )
        assert not ok and len(attempts) == 2

    @pytest.mark.asyncio
    async def test_missing_key_or_url_drops(self):
        a, _ = _assignment()
        assert not await submit_response(
            _completed(a), leader_api_url="", private_key=_PRIV,
            deadline_epoch=10, current_epoch_fn=lambda: 1,
        )
        assert not await submit_response(
            _completed(a), leader_api_url="http://x", private_key=None,
            deadline_epoch=10, current_epoch_fn=lambda: 1,
        )
