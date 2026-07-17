"""Distributed-veto Phase 0 observe layer: phase_observe_counts, the fan-out
exclude + K-consecutive-reject streak, leader re-verification, observe_summary,
and RoundState.veto_observe persistence.
"""

from __future__ import annotations

import json
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
    async def test_include_responders_benches_only_selected(self):
        # STREAMING: a completing pass benches ONLY the not-yet-covered responder,
        # so an already-covered veto is never re-benched (bench-once).
        a1, o1 = _assignment("0xv1", order_ids=("ord_a",))
        a2, o2 = _assignment("0xv2", order_ids=("ord_b",))
        ph = _phase([a1, a2])
        ph.responses["0xv1"] = _resp(a1, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        ph.responses["0xv2"] = _resp(a2, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_b", "dropped", "2000000", ""),
        ])
        orders = {**o1, **o2}
        # Restrict to v2 only: plan must be ord_b, NOT ord_a.
        champ = [_row("ord_b", "2000000")]
        chal = [_row("ord_b", None)]
        out = await reverify_dissents(
            ph, include_responders={"0xv2"}, **_reverify_env(orders, champ, chal),
        )
        assert out["planned"] == 1 and "ord_b" in out["orders"]
        assert "ord_a" not in out["orders"]

    @pytest.mark.asyncio
    async def test_include_responders_empty_set_plans_nothing(self):
        a, orders = _assignment(order_ids=("ord_a",))
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        out = await reverify_dissents(
            ph, include_responders=set(), **_reverify_env(orders, [], []),
        )
        assert out["ran"] is False and out["planned"] == 0

    @pytest.mark.asyncio
    async def test_reverify_benches_each_chain_at_its_own_pin(self):
        # MULTI-CHAIN batch: two responders veto on DIFFERENT chains in one pass. The
        # leader must bench EACH chain at its own pin and confirm both — NOT bail on the
        # mixed batch (the fail-open regression the multi-chain veto would otherwise
        # introduce: a dual-chain-regressing challenger escaping the veto).
        base_rec = {**_order("ord_base"), "chain_id": 8453}
        eth_rec = {**_order("ord_eth"), "chain_id": 1}
        pins = {"1": 25_000_000, "8453": 28_000_000}

        def _mk(evm, oid, rec):
            return SliceAssignment(
                round_id="round-e9-n1", slice_index=0, validator_evm=evm,
                candidate_submission_id="sub_1",
                candidate_image_id=_DA, incumbent_image_id=_DB,
                candidate_image_ref=f"ghcr.io/x@sha256:{_DA}",
                incumbent_image_ref=f"ghcr.io/x@sha256:{_DB}",
                order_ids=[oid], order_hashes={oid: order_replay_hash(rec)},
                calibration_order_ids=[], fork_pins=pins, deadline_epoch=100,
                leader_api_url="http://leader:8080",
            )

        ph = _phase([_mk("0xv1", "ord_base", base_rec), _mk("0xv2", "ord_eth", eth_rec)])
        ph.responses["0xv1"] = _resp(ph.assignments[0], verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_base", "dropped", "1000000", ""),
        ])
        ph.responses["0xv2"] = _resp(ph.assignments[1], verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_eth", "dropped", "2000000", ""),
        ])
        orders = {"ord_base": base_rec, "ord_eth": eth_rec}
        # Chains bench in sorted str order: "1"(ETH) then "8453"(Base). Both regressions
        # reproduce (champ delivers, challenger drops).
        worker = SimpleNamespace()
        worker.benchmark_explicit_orders = AsyncMock(side_effect=[
            [_row("ord_eth", "2000000")], [_row("ord_eth", None)],     # ETH: champ, chal
            [_row("ord_base", "1000000")], [_row("ord_base", None)],   # Base: champ, chal
        ])

        async def pull(ref):
            return True

        out = await reverify_dissents(
            ph, order_lookup=lambda oid: orders.get(oid),
            worker_factory=lambda: worker, pull_image=pull,
        )
        assert out["ran"] is True and out["planned"] == 2
        assert out["confirmed"] == 2          # BOTH chains confirmed (no mixed-batch no-op)
        assert out["orders"]["ord_base"] is True
        assert out["orders"]["ord_eth"] is True
        assert out["benched"] == {"ord_base", "ord_eth"}


class TestMergeReverifyResults:
    """Accumulating disjoint streaming re-verify passes into one running result."""

    def test_first_pass_is_copied(self):
        new = {"ran": True, "planned": 1, "confirmed": 1, "discarded": 0,
               "orders": {"ord_a": True}}
        out = veto_wire.merge_reverify_results(None, new)
        assert out == new and out is not new  # copy, not alias

    def test_union_recomputes_confirmed(self):
        prev = {"ran": True, "planned": 1, "confirmed": 0, "discarded": 1,
                "orders": {"ord_a": False}}
        new = {"ran": True, "planned": 1, "confirmed": 1, "discarded": 0,
               "orders": {"ord_b": True}}
        out = veto_wire.merge_reverify_results(prev, new)
        assert out["orders"] == {"ord_a": False, "ord_b": True}
        assert out["planned"] == 2 and out["confirmed"] == 1 and out["discarded"] == 1

    def test_prior_confirmation_survives_a_later_noop_pass(self):
        # REGRESSION GUARD: a confirmed block must never be dropped by a later
        # crashed/no-op pass (would_gate_confirmed must stay derivable as True).
        prev = {"ran": True, "planned": 1, "confirmed": 1, "discarded": 0,
                "orders": {"ord_a": True}}
        out = veto_wire.merge_reverify_results(prev, None)
        assert out["confirmed"] == 1 and out["orders"]["ord_a"] is True
        assert out["ran"] is True

    def test_noop_first_then_confirm(self):
        prev = veto_wire.merge_reverify_results(None, None)  # crashed first pass
        assert prev["ran"] is False and prev["confirmed"] == 0
        out = veto_wire.merge_reverify_results(prev, {
            "ran": True, "planned": 1, "confirmed": 1, "discarded": 0,
            "orders": {"ord_b": True}})
        assert out["ran"] is True and out["confirmed"] == 1

    def test_strips_transient_benched_set_and_stays_json_safe(self):
        # REGRESSION GUARD: reverify_dissents returns a `benched` SET for coverage;
        # it must never survive into the merged/persisted record (json.dumps would
        # choke on a set). merge rebuilds a clean 5-key dict on BOTH branches.
        raw = {"ran": True, "planned": 1, "confirmed": 1, "discarded": 0,
               "orders": {"o1": True}, "benched": {"o1"}}
        first = veto_wire.merge_reverify_results(None, raw)   # first-pass branch
        second = veto_wire.merge_reverify_results(first, raw)  # union branch
        for out in (first, second):
            assert "benched" not in out
            json.dumps(out)  # must not raise

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


# ── streaming coverage (end-to-end composition) ──────────────────────────────

class TestStreamingCoverage:
    """Drives the REAL pure building blocks (veto_stream_action + reverify_dissents
    with include_responders + merge_reverify_results + observe_summary) in the
    exact order _veto_stream_step composes them, to lock the coverage property the
    adversarial review flagged: a confirmable veto from a LATE follower must still
    be leader-reproduced (would_gate_confirmed=True), never fail-opened."""

    async def _drive_once(self, ph, orders, st, now, *, benches):
        # Mirror _veto_stream_step + the completion for one step: decide, and on
        # SPAWN run the (mocked) re-verify for the uncovered responders, merge it,
        # and — exactly as the real completion — commit coverage ONLY on ran=True.
        veto_responders = {
            evm for evm, r in ph.responses.items() if r.verdict == VERDICT_VETO
        }
        result = st.get("result")
        confirmed = bool(result and result.get("confirmed", 0) > 0)
        action, resolution = veto_wire.resolve_phase(ph, now)
        resolved = action == "resolve"
        act = veto_wire.veto_stream_action(
            reverify_enabled=True, has_veto=bool(veto_responders),
            resolved=resolved, inflight=False,
            uncovered=bool(veto_responders - st["covered"]),
            result_present=result is not None, confirmed=confirmed,
        )
        if act == veto_wire.STREAM_SPAWN:
            new = veto_responders - st["covered"]
            champ, chal = benches[frozenset(new)]
            out = await reverify_dissents(
                ph, include_responders=new, **_reverify_env(orders, champ, chal),
            )
            st["result"] = veto_wire.merge_reverify_results(st.get("result"), out)
            # per-responder coverage on ACTUALLY-benched orders (mirror completion)
            benched = set(out.get("benched") or ())
            for evm in new:
                resp = ph.responses.get(evm)
                if resp is None:
                    continue
                pids = {v.order_id for v in veto_wire.plan_reverification(resp)}
                if pids and pids <= benched:
                    st["covered"].add(evm)
            return "spawned"
        if act in (veto_wire.STREAM_WRITE_RESULT, veto_wire.STREAM_WRITE_NONE):
            rv = st["result"] if act == veto_wire.STREAM_WRITE_RESULT else None
            return observe_summary(ph.assignments[0].round_id, ph,
                                   resolution or "reverified", rv)
        return act

    @pytest.mark.asyncio
    async def test_late_confirmable_veto_still_blocks(self):
        # F1 (fast) vetoes order_a — NOT leader-reproducible (flaky).
        # F2 (slow) vetoes order_b — leader-reproducible (real regression).
        a1, o1 = _assignment("0xv1", order_ids=("ord_a",))
        a2, o2 = _assignment("0xv2", order_ids=("ord_b",))
        orders = {**o1, **o2}
        ph = _phase([a1, a2])
        st = {"covered": set(), "inflight": False, "result": None, "done": False}
        benches = {
            frozenset({"0xv1"}): ([_row("ord_a", "1000000")], [_row("ord_a", "1000000")]),  # matched → not confirmed
            frozenset({"0xv2"}): ([_row("ord_b", "2000000")], [_row("ord_b", None)]),        # dropped → confirmed
        }
        # tick 1: only F1 has vetoed (phase not resolved — F2 outstanding)
        ph.responses["0xv1"] = _resp(a1, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        assert await self._drive_once(ph, orders, st, now=50, benches=benches) == "spawned"
        assert st["result"]["confirmed"] == 0  # flaky claim discarded

        # tick 2: F2's real veto now arrives and the phase resolves (all terminal)
        ph.responses["0xv2"] = _resp(a2, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_b", "dropped", "2000000", ""),
        ])
        assert await self._drive_once(ph, orders, st, now=60, benches=benches) == "spawned"
        assert st["result"]["confirmed"] == 1  # F2's regression reproduced

        # finalize: confirmed → terminal observe with would_gate_confirmed True
        summary = await self._drive_once(ph, orders, st, now=61, benches=benches)
        assert summary["would_gate_confirmed"] is True
        assert summary["reverify"]["orders"] == {"ord_a": False, "ord_b": True}

    @pytest.mark.asyncio
    async def test_junk_first_batch_does_not_starve_a_real_veto(self):
        # REGRESSION GUARD (finding #1): a junk follower fills its own budget with
        # 10 fabricated claims; a real follower co-batched in the SAME pass must
        # still have its 1 confirmable order benched (no global union-truncation).
        junk_ids = tuple(f"ord_j{i}" for i in range(10))
        a1, oj = _assignment("0xvjunk", order_ids=junk_ids)
        a2, orl = _assignment("0xvreal", order_ids=("ord_real",))
        orders = {**oj, **orl}
        ph = _phase([a1, a2])
        ph.responses["0xvjunk"] = _resp(a1, verdict=VERDICT_VETO, violations=[
            SliceViolation(o, "dropped", "1", "") for o in junk_ids
        ])
        ph.responses["0xvreal"] = _resp(a2, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_real", "dropped", "5000000", ""),
        ])
        # leader reproduces NONE of the junk (matched) but DOES reproduce ord_real
        all_ids = list(junk_ids) + ["ord_real"]
        champ = [_row(o, "1000000") for o in junk_ids] + [_row("ord_real", "5000000")]
        chal = [_row(o, "1000000") for o in junk_ids] + [_row("ord_real", None)]

        def wf():
            w = SimpleNamespace()
            w.benchmark_explicit_orders = AsyncMock(side_effect=[champ, chal])
            return w

        env = {"order_lookup": lambda oid: orders.get(oid),
               "worker_factory": wf, "pull_image": _reverify_env({}, [], [])["pull_image"]}
        out = await reverify_dissents(
            ph, include_responders={"0xvjunk", "0xvreal"}, **env,
        )
        # ord_real survived the plan (not truncated) and was confirmed → BLOCK.
        assert "ord_real" in out["orders"] and out["orders"]["ord_real"] is True
        assert out["confirmed"] == 1 and out["planned"] == 11

    @pytest.mark.asyncio
    async def test_noop_pass_does_not_cover_responder(self):
        # REGRESSION GUARD (finding #2): a ran=False (transient gap) pass must NOT
        # mark its responder covered — it stays uncovered so the next tick retries,
        # never a silent covered-but-unbenched fail-open allow.
        a, orders = _assignment("0xv1", order_ids=("ord_a",))
        ph = _phase([a])
        ph.responses["0xv1"] = _resp(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        st = {"covered": set(), "inflight": False, "result": None, "done": False}
        # First pass: bench raises → reverify_dissents swallows → ran=False.
        def wf_boom():
            w = SimpleNamespace()
            w.benchmark_explicit_orders = AsyncMock(side_effect=RuntimeError("boom"))
            return w
        benches_boom = {frozenset({"0xv1"}): (None, None)}  # unused; wf raises
        # drive with a raising worker
        veto_responders = {"0xv1"}
        out = await reverify_dissents(
            ph, include_responders={"0xv1"},
            order_lookup=lambda oid: orders.get(oid),
            worker_factory=wf_boom,
            pull_image=_reverify_env({}, [], [])["pull_image"],
        )
        st["result"] = veto_wire.merge_reverify_results(None, out)
        benched = set(out.get("benched") or ())
        for evm in {"0xv1"}:
            pids = {v.order_id for v in veto_wire.plan_reverification(
                ph.responses[evm])}
            if pids and pids <= benched:
                st["covered"].add(evm)
        assert out["ran"] is False
        # v1 stays UNCOVERED → uncovered still true → the gate re-spawns / holds,
        # never finalizes-allow on the gapped pass.
        assert st["covered"] == set()
        assert veto_wire.veto_stream_action(
            reverify_enabled=True, has_veto=True, resolved=True, inflight=False,
            uncovered=bool(veto_responders - st["covered"]),
            result_present=st["result"] is not None, confirmed=False,
        ) == veto_wire.STREAM_SPAWN

    @pytest.mark.asyncio
    async def test_partial_lookup_miss_leaves_that_responder_uncovered(self):
        # REGRESSION GUARD (finding A): responder B's order misses order_lookup while
        # co-batched responder A benches fine (ran=True). B must NOT be marked
        # covered (its order was never benched) — it stays uncovered for retry.
        a1, o1 = _assignment("0xva", order_ids=("ord_a",))
        a2, o2 = _assignment("0xvb", order_ids=("ord_b",))
        ph = _phase([a1, a2])
        ph.responses["0xva"] = _resp(a1, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "dropped", "1000000", ""),
        ])
        ph.responses["0xvb"] = _resp(a2, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_b", "dropped", "2000000", ""),
        ])
        # order_lookup returns A's order but MISSES B's (transient store gap).
        only_a = {"ord_a": o1["ord_a"]}
        champ = [_row("ord_a", "1000000")]
        chal = [_row("ord_a", "1000000")]  # A matched → not confirmed

        def wf():
            w = SimpleNamespace()
            w.benchmark_explicit_orders = AsyncMock(side_effect=[champ, chal])
            return w

        out = await reverify_dissents(
            ph, include_responders={"0xva", "0xvb"},
            order_lookup=lambda oid: only_a.get(oid),
            worker_factory=wf,
            pull_image=_reverify_env({}, [], [])["pull_image"],
        )
        assert out["ran"] is True and out["benched"] == {"ord_a"}
        # simulate the completion's per-responder coverage
        covered = set()
        for evm in {"0xva", "0xvb"}:
            pids = {v.order_id for v in veto_wire.plan_reverification(
                ph.responses[evm])}
            if pids and pids <= set(out["benched"]):
                covered.add(evm)
        assert covered == {"0xva"}  # B (unbenched) stays UNCOVERED → will retry


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
