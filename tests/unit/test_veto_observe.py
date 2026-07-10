"""Distributed-veto Phase 0 observe layer: phase_observe_counts, the fan-out
exclude + K-consecutive-reject streak, leader re-verification, observe_summary,
and RoundState.veto_observe persistence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from minotaur_subnet.api.routes.submissions import veto_wire
from minotaur_subnet.api.routes.submissions.veto_wire import (
    K_CONSECUTIVE_UNSUPPORTED,
    consecutive_reject_terminal,
    fan_out_assignments,
    observe_summary,
    reverify_dissents,
)
from minotaur_subnet.epoch.distributed_veto import (
    STATUS_FAILED,
    STATUS_REFUSED,
    VERDICT_OK,
    VERDICT_VETO,
    SliceAssignment,
    SliceViolation,
    VetoPhaseState,
    VetoResponse,
    phase_observe_counts,
)
from minotaur_subnet.harness.order_sampler import order_replay_hash

_DA, _DB = "a" * 64, "b" * 64


def _order(oid: str) -> dict:
    return {
        "order_id": oid, "app_id": "app_dex", "chain_id": 8453,
        "status": "filled", "intent_function": "swap",
        "params": {"input_token": "0xW", "output_token": f"0xO_{oid}",
                   "input_amount": "1000000000000000000"},
    }


def _assignment(validator="0xv1", order_ids=("ord_a", "ord_b"), calib=()):
    orders = {o: _order(o) for o in list(order_ids) + list(calib)}
    return SliceAssignment(
        round_id="round-e9-n1", slice_index=0, validator_evm=validator,
        candidate_submission_id="sub_1",
        candidate_image_id=_DA, incumbent_image_id=_DB,
        candidate_image_ref=f"ghcr.io/x@sha256:{_DA}",
        incumbent_image_ref=f"ghcr.io/x@sha256:{_DB}",
        order_ids=list(order_ids),
        order_hashes={o: order_replay_hash(r) for o, r in orders.items()},
        calibration_order_ids=list(calib),
        fork_pins={"8453": 28_000_000}, deadline_epoch=100,
        leader_api_url="http://leader:8080",
    ), orders


def _resp(a, *, status="completed", verdict=VERDICT_OK, violations=None):
    return VetoResponse(
        assignment_id=a.assignment_id, round_id=a.round_id,
        validator_evm=a.validator_evm, status=status, verdict=verdict,
        violations=violations or [],
    )


def _phase(assignments):
    return VetoPhaseState(
        candidate_submission_id="sub_1", candidate_image_id=_DA,
        deadline_epoch=100, assignments=list(assignments),
    )


@pytest.fixture(autouse=True)
def _clean():
    veto_wire._reset_send_streaks()
    yield
    veto_wire._reset_send_streaks()


# ── phase_observe_counts ──────────────────────────────────────────────────────

class TestObserveCounts:
    def test_tallies_every_status(self):
        a1, _ = _assignment("0xv1")
        a2, _ = _assignment("0xv2")
        a3, _ = _assignment("0xv3")
        a4, _ = _assignment("0xv4")
        ph = _phase([a1, a2, a3, a4])
        ph.responses["0xv1"] = _resp(a1, verdict=VERDICT_OK)
        ph.responses["0xv2"] = _resp(a2, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1", "0"),
            SliceViolation("ord_b", "catastrophic", "1000", "800"),
        ])
        ph.responses["0xv3"] = _resp(a3, status=STATUS_FAILED, verdict=None)
        ph.unsupported.append("0xv4")
        c = phase_observe_counts(ph)
        assert c["n_assignments"] == 4
        assert c["n_ok"] == 1
        assert c["n_veto"] == 1
        assert c["n_claimed_violations"] == 2
        assert c["n_failed"] == 1
        assert c["n_unsupported"] == 1

    def test_responded_not_double_counted_as_unsupported(self):
        a1, _ = _assignment("0xv1")
        ph = _phase([a1])
        ph.responses["0xv1"] = _resp(a1, status=STATUS_REFUSED, verdict=None)
        ph.unsupported.append("0xv1")  # both a response AND flagged
        c = phase_observe_counts(ph)
        assert c["n_refused"] == 1
        assert c["n_unsupported"] == 0  # a responder is not an abstainer


# ── fan-out exclude + K-consecutive-reject ───────────────────────────────────

class TestFanOutStreak:
    @pytest.mark.asyncio
    async def test_exclude_skips_responders(self):
        a1, _ = _assignment("0xv1")
        a2, _ = _assignment("0xv2")
        sent = []

        async def post(url, payload):
            sent.append(payload["validator_evm"])
            return 200, {}

        out = await fan_out_assignments(
            [a1, a2], peer_urls={"0xv1": "http://v1", "0xv2": "http://v2"},
            sign_payload=lambda p: p, exclude={"0xv1"}, post_json=post,
        )
        assert sent == ["0xv2"]
        assert set(out) == {"0xv2"}

    @pytest.mark.asyncio
    async def test_terminal_only_after_k_consecutive_rejects(self):
        a1, _ = _assignment("0xv1")

        async def reject(url, payload):
            return 404, {}

        rid = a1.round_id
        for i in range(1, K_CONSECUTIVE_UNSUPPORTED):
            await fan_out_assignments(
                [a1], peer_urls={"0xv1": "http://v1"},
                sign_payload=lambda p: p, post_json=reject,
            )
            assert not consecutive_reject_terminal(rid, "0xv1"), f"iter {i}"
        # Kth reject flips terminal
        await fan_out_assignments(
            [a1], peer_urls={"0xv1": "http://v1"},
            sign_payload=lambda p: p, post_json=reject,
        )
        assert consecutive_reject_terminal(rid, "0xv1")

    @pytest.mark.asyncio
    async def test_black_hole_peer_does_not_serialize_block(self):
        # A hung peer must not make fan-out take N×timeout: sends run concurrently
        # and each is bounded, so total ≈ one timeout, not the sum.
        import asyncio as _a
        a1, _ = _assignment("0xv1")
        a2, _ = _assignment("0xv2")
        a3, _ = _assignment("0xv3")

        async def slow(url, payload):
            await _a.sleep(0.2)  # simulates a peer that answers slowly
            return 200, {}

        import time
        t0 = time.monotonic()
        out = await fan_out_assignments(
            [a1, a2, a3],
            peer_urls={"0xv1": "http://v1", "0xv2": "http://v2", "0xv3": "http://v3"},
            sign_payload=lambda p: p, post_json=slow,
        )
        elapsed = time.monotonic() - t0
        assert set(out.values()) == {"acked"}
        assert elapsed < 0.5, "sends must run concurrently, not serially"

    @pytest.mark.asyncio
    async def test_forget_round_send_state_clears_streaks(self):
        a1, _ = _assignment("0xv1")
        rid = a1.round_id

        async def reject(url, payload):
            return 404, {}

        for _ in range(veto_wire.K_CONSECUTIVE_UNSUPPORTED):
            await fan_out_assignments([a1], peer_urls={"0xv1": "http://v1"},
                                      sign_payload=lambda p: p, post_json=reject)
        assert consecutive_reject_terminal(rid, "0xv1")
        veto_wire.forget_round_send_state(rid)
        assert not consecutive_reject_terminal(rid, "0xv1")

    @pytest.mark.asyncio
    async def test_transient_or_success_resets_streak(self):
        a1, _ = _assignment("0xv1")
        rid = a1.round_id

        async def reject(url, payload):
            return 409, {}

        async def down(url, payload):
            raise ConnectionError("x")

        for _ in range(K_CONSECUTIVE_UNSUPPORTED - 1):
            await fan_out_assignments([a1], peer_urls={"0xv1": "http://v1"},
                                      sign_payload=lambda p: p, post_json=reject)
        # a network blip resets — one deterministic reject can't be terminal now
        await fan_out_assignments([a1], peer_urls={"0xv1": "http://v1"},
                                  sign_payload=lambda p: p, post_json=down)
        await fan_out_assignments([a1], peer_urls={"0xv1": "http://v1"},
                                  sign_payload=lambda p: p, post_json=reject)
        assert not consecutive_reject_terminal(rid, "0xv1")


# ── leader re-verification ───────────────────────────────────────────────────

def _reverify_env(orders, champ, chal, *, pull_ok=True):
    async def pull(ref):
        return pull_ok

    worker = SimpleNamespace()
    worker.benchmark_explicit_orders = AsyncMock(side_effect=[champ, chal])
    return {
        "order_lookup": lambda oid: orders.get(oid),
        "worker_factory": lambda: worker,
        "pull_image": pull,
    }


def _row(oid, raw, app="app_dex"):
    return SimpleNamespace(intent_id=f"{app}:hist:{oid}", raw_output=raw, error=None)


class TestReverify:
    @pytest.mark.asyncio
    async def test_confirms_reproduced_violation(self):
        a, orders = _assignment(order_ids=("ord_a", "ord_b"))
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        # leader reproduces: champion delivers ord_a, challenger drops it
        champ = [_row("ord_a", "1000000")]
        chal = [_row("ord_a", None)]
        out = await reverify_dissents(ph, **_reverify_env(orders, champ, chal))
        assert out["ran"] and out["planned"] == 1
        assert out["confirmed"] == 1 and out["discarded"] == 0
        assert out["orders"]["ord_a"] is True

    @pytest.mark.asyncio
    async def test_discards_irreproducible_claim(self):
        a, orders = _assignment(order_ids=("ord_a",))
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        # leader does NOT reproduce: both deliver equally (matched)
        champ = [_row("ord_a", "1000000")]
        chal = [_row("ord_a", "1000000")]
        out = await reverify_dissents(ph, **_reverify_env(orders, champ, chal))
        assert out["ran"] and out["confirmed"] == 0 and out["discarded"] == 1
        assert out["orders"]["ord_a"] is False

    @pytest.mark.asyncio
    async def test_no_vetoes_is_noop(self):
        a, orders = _assignment()
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_OK)
        out = await reverify_dissents(ph, **_reverify_env(orders, [], []))
        assert out["ran"] is False and out["planned"] == 0

    @pytest.mark.asyncio
    async def test_bench_failure_is_swallowed(self):
        a, orders = _assignment(order_ids=("ord_a",))
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        env = _reverify_env(orders, [], [])
        env["worker_factory"]().benchmark_explicit_orders = AsyncMock(
            side_effect=RuntimeError("boom"),
        )

        def wf():
            w = SimpleNamespace()
            w.benchmark_explicit_orders = AsyncMock(side_effect=RuntimeError("boom"))
            return w

        env["worker_factory"] = wf
        out = await reverify_dissents(ph, **env)
        assert out["ran"] is False  # never raises


# ── observe summary ──────────────────────────────────────────────────────────

class TestObserveSummary:
    def test_claims_upper_bound_when_reverify_off(self):
        a, _ = _assignment()
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1", "0"),
        ])
        s = observe_summary("round-e9-n1", ph, "all_terminal", None)
        # a claimed veto with reverify off: claims-upper-bound True, confirmed
        # is Null (NOT a Phase-1 prediction — LD 8 needs leader confirmation)
        assert s["would_gate_claims"] is True
        assert s["would_gate_confirmed"] is None
        assert s["n_veto"] == 1
        assert s["resolution"] == "all_terminal"

    def test_confirmed_needs_leader_reverification(self):
        a, _ = _assignment()
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1", "0"),
        ])
        # reverify ran but confirmed nothing → claims True but confirmed False
        s = observe_summary("round-e9-n1", ph, "all_terminal",
                            {"ran": True, "planned": 1, "confirmed": 0, "discarded": 1})
        assert s["would_gate_claims"] is True
        assert s["would_gate_confirmed"] is False
        # reverify confirmed → confirmed True
        s2 = observe_summary("round-e9-n1", ph, "all_terminal",
                             {"ran": True, "planned": 1, "confirmed": 1, "discarded": 0})
        assert s2["would_gate_confirmed"] is True

    def test_summary_is_json_compact(self):
        import json
        a, _ = _assignment()
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_OK)
        s = observe_summary("round-e9-n1", ph, "all_terminal", None)
        blob = json.dumps(s)  # must serialize
        assert "per_intent" not in blob and "raw_output" not in blob


def test_observe_summary_includes_confirmed_first_violations():
    # observe_summary surfaces the per-order regressions followers flagged, each
    # tagged confirmed (leader reproduced), confirmed-first for the miner report.
    a, _ = _assignment(order_ids=("ord_a", "ord_b"))
    ph = _phase([a])
    ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
        SliceViolation("ord_b", "catastrophic", "1000", "800"),
        SliceViolation("ord_a", "dropped", "1", "0"),
    ])
    reverify = {"ran": True, "confirmed": 1, "discarded": 1,
                "orders": {"ord_a": True, "ord_b": False}}
    s = observe_summary("round-x", ph, "all_terminal", reverify)
    v = s["violations"]
    assert {x["order_id"] for x in v} == {"ord_a", "ord_b"}
    assert v[0]["order_id"] == "ord_a" and v[0]["confirmed"] is True   # confirmed-first
    assert v[1]["confirmed"] is False
    assert v[0]["champ_raw"] == "1" and v[0]["chal_raw"] == "0"


def test_observe_summary_no_reverify_marks_unconfirmed():
    a, _ = _assignment(order_ids=("ord_a",))
    ph = _phase([a])
    ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
        SliceViolation("ord_a", "dropped", "1", "0"),
    ])
    s = observe_summary("round-x", ph, "all_terminal", None)  # reverify not run
    assert s["would_gate_confirmed"] is None
    assert s["violations"][0]["confirmed"] is False
