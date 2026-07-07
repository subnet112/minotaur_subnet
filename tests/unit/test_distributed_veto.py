"""Distributed-veto protocol logic (epoch/distributed_veto — pure, Phase 0).

Pins the liveness invariants (zero-assignment ⇒ same-tick resolve; every
follower failure mode ⇒ abstain/terminal, never a stall), the identity
bindings (assignment_id keyed on round + candidate digest + slice content),
the ingestion caps, and the deterministic entropy-seeded selection.
"""

from __future__ import annotations

import pytest

from minotaur_subnet.epoch.distributed_veto import (
    ACTION_NOOP,
    ACTION_RESOLVE,
    ACTION_WAIT,
    MAX_CALIBRATION_ROWS,
    MAX_VIOLATIONS_PER_RESPONSE,
    RESOLUTION_ALL_TERMINAL,
    RESOLUTION_NO_ASSIGNMENTS,
    RESOLUTION_WINDOW_ELAPSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REFUSED,
    VERDICT_OK,
    VERDICT_VETO,
    VETO_VERIFY_BUDGET_ORDERS,
    SliceAssignment,
    SliceViolation,
    VetoPhaseState,
    VetoResponse,
    assign_slices,
    pick_audit_orders,
    pick_audit_target,
    plan_reverification,
    resolve_phase,
    validate_response,
    verify_assignment_integrity,
)


def _assignment(validator="0xAbCd", order_ids=None, deadline=100) -> SliceAssignment:
    ids = order_ids if order_ids is not None else ["ord_a", "ord_b", "ord_c"]
    return SliceAssignment(
        round_id="round-e9-n1",
        slice_index=1,
        validator_evm=validator.lower(),
        candidate_submission_id="sub_123",
        candidate_image_id="a" * 64,
        incumbent_image_id="b" * 64,
        order_ids=list(ids),
        order_hashes={oid: f"h_{oid}" for oid in ids},
        calibration_order_ids=["cal_1"],
        fork_pins={"8453": 28000000},
        deadline_epoch=deadline,
    )


def _response(assignment, *, status=STATUS_COMPLETED, verdict=VERDICT_OK,
              violations=None, calibration=None, error=None) -> VetoResponse:
    return VetoResponse(
        assignment_id=assignment.assignment_id,
        round_id=assignment.round_id,
        validator_evm=assignment.validator_evm,
        status=status,
        verdict=verdict,
        violations=violations or [],
        counts={"wins": 1},
        calibration=calibration or [],
        error=error,
    )


# ── assignment identity ───────────────────────────────────────────────────────

class TestAssignmentIdentity:
    def test_payload_round_trip(self):
        a = _assignment()
        b = SliceAssignment.from_payload(a.to_payload())
        assert b == a
        assert b.assignment_id == a.assignment_id

    def test_integrity_check_passes_and_detects_tamper(self):
        payload = _assignment().to_payload()
        ok, _ = verify_assignment_integrity(payload)
        assert ok

        reordered = dict(payload)
        reordered["order_ids"] = list(reversed(payload["order_ids"]))
        ok, reason = verify_assignment_integrity(reordered)
        assert not ok and "slice_hash" in reason

        swapped = dict(payload)
        swapped["order_hashes"] = dict(payload["order_hashes"], ord_a="h_tampered")
        ok, _ = verify_assignment_integrity(swapped)
        assert not ok

    def test_assignment_id_binds_candidate_and_slice(self):
        a = _assignment()
        other_candidate = _assignment()
        other_candidate.candidate_image_id = "c" * 64
        assert a.assignment_id != other_candidate.assignment_id

        other_slice = _assignment(order_ids=["ord_z"])
        assert a.assignment_id != other_slice.assignment_id

        # ...but NOT the validator: a re-send of the same work to a different
        # validator is the same assignment content.
        other_validator = _assignment(validator="0xFFFF")
        assert a.assignment_id == other_validator.assignment_id

    def test_empty_slice_rejected(self):
        payload = _assignment(order_ids=[]).to_payload()
        ok, reason = verify_assignment_integrity(payload)
        assert not ok and "empty" in reason


# ── response validation (ingestion binding + caps) ───────────────────────────

class TestResponseValidation:
    def test_valid_ok_and_veto(self):
        a = _assignment()
        ok, _ = validate_response(_response(a), a)
        assert ok
        veto = _response(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "catastrophic", "1000", "900"),
        ])
        ok, _ = validate_response(veto, a)
        assert ok

    def test_binding_mismatches_rejected(self):
        a = _assignment()
        r = _response(a)
        r.assignment_id = "0" * 32
        assert not validate_response(r, a)[0]

        r = _response(a)
        r.round_id = "round-e9-n2"
        assert not validate_response(r, a)[0]

        r = _response(a)
        r.validator_evm = "0xother"
        assert not validate_response(r, a)[0]

    def test_status_and_verdict_shape(self):
        a = _assignment()
        r = _response(a, status="weird")
        assert not validate_response(r, a)[0]
        # completed needs a verdict
        r = _response(a, verdict=None)
        assert not validate_response(r, a)[0]
        # non-completed must NOT carry one
        r = _response(a, status=STATUS_REFUSED, verdict=VERDICT_OK)
        assert not validate_response(r, a)[0]
        r = _response(a, status=STATUS_FAILED, verdict=None, error="bench_error")
        assert validate_response(r, a)[0]

    def test_violations_must_reference_slice_orders(self):
        a = _assignment()
        r = _response(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_NOT_IN_SLICE", "dropped", "1", "0"),
        ])
        ok, reason = validate_response(r, a)
        assert not ok and "outside the slice" in reason

    def test_violations_require_veto_verdict(self):
        a = _assignment()
        r = _response(a, verdict=VERDICT_OK, violations=[
            SliceViolation("ord_a", "dropped", "1", "0"),
        ])
        assert not validate_response(r, a)[0]

    def test_veto_requires_violations(self):
        # An evidence-free veto is unverifiable by construction — rejected at
        # ingestion, never a phantom OK at the planner.
        a = _assignment()
        r = _response(a, verdict=VERDICT_VETO, violations=[])
        ok, reason = validate_response(r, a)
        assert not ok and "no violations" in reason

    def test_size_caps(self):
        ids = [f"ord_{i}" for i in range(MAX_VIOLATIONS_PER_RESPONSE + 1)]
        a = _assignment(order_ids=ids)
        r = _response(a, verdict=VERDICT_VETO, violations=[
            SliceViolation(oid, "dropped", "1", "0") for oid in ids
        ])
        ok, reason = validate_response(r, a)
        assert not ok and "too many violations" in reason

        a = _assignment()
        r = _response(a, calibration=[
            {"order_id": "cal_1", "champ_raw": "1", "chal_raw": "1"}
        ] * (MAX_CALIBRATION_ROWS + 1))
        ok, reason = validate_response(r, a)
        assert not ok and "calibration" in reason

    def test_calibration_rows_bound_to_calibration_ids(self):
        a = _assignment()
        r = _response(a, calibration=[
            {"order_id": "ord_a", "champ_raw": "1", "chal_raw": "1"},
        ])
        assert not validate_response(r, a)[0]


# ── phase resolution (the liveness core) ─────────────────────────────────────

class TestResolvePhase:
    def _phase(self, assignments, deadline=100) -> VetoPhaseState:
        return VetoPhaseState(
            candidate_submission_id="sub_123",
            candidate_image_id="a" * 64,
            deadline_epoch=deadline,
            assignments=assignments,
        )

    def test_zero_assignments_resolves_same_tick(self):
        # THE quorum-1 liveness invariant: no reachable peers / no pushed digest
        # / feature unarmed ⇒ the phase must cost ZERO epochs, not a vacuous
        # deadline wait.
        phase = self._phase([])
        action, resolution = resolve_phase(phase, current_epoch=0)
        assert action == ACTION_RESOLVE
        assert resolution == RESOLUTION_NO_ASSIGNMENTS

    def test_all_terminal_early_resolves_before_deadline(self):
        a1, a2 = _assignment("0xV1"), _assignment("0xV2")
        phase = self._phase([a1, a2], deadline=100)
        phase.responses["0xv1"] = _response(a1)
        phase.responses["0xv2"] = _response(
            a2, status=STATUS_REFUSED, verdict=None, error="corpus_mismatch",
        )
        action, resolution = resolve_phase(phase, current_epoch=5)
        assert action == ACTION_RESOLVE
        assert resolution == RESOLUTION_ALL_TERMINAL

    def test_unsupported_counts_as_terminal(self):
        # :stable followers 404ing the endpoint must become terminal abstains,
        # or arming before promotion stalls every finalist round to deadline.
        a1, a2 = _assignment("0xV1"), _assignment("0xV2")
        phase = self._phase([a1, a2], deadline=100)
        phase.responses["0xv1"] = _response(a1)
        phase.unsupported.append("0xv2")
        action, resolution = resolve_phase(phase, current_epoch=5)
        assert action == ACTION_RESOLVE
        assert resolution == RESOLUTION_ALL_TERMINAL

    def test_outstanding_waits_then_window_elapses(self):
        a1 = _assignment("0xV1")
        phase = self._phase([a1], deadline=100)
        assert resolve_phase(phase, current_epoch=100) == (ACTION_WAIT, None)
        action, resolution = resolve_phase(phase, current_epoch=101)
        assert action == ACTION_RESOLVE
        assert resolution == RESOLUTION_WINDOW_ELAPSED

    def test_terminality_bound_to_assignment_not_validator(self):
        # Restored/degenerate state can hold two assignments for one validator;
        # a terminal response for slice A must not early-resolve slice B.
        a1 = _assignment("0xV1")
        a2 = _assignment("0xV1", order_ids=["ord_z1", "ord_z2"])
        assert a1.assignment_id != a2.assignment_id
        phase = self._phase([a1, a2], deadline=100)
        phase.responses["0xv1"] = _response(a1)
        assert resolve_phase(phase, current_epoch=5) == (ACTION_WAIT, None)

    def test_stale_candidate_response_does_not_terminate(self):
        # A response bound to a PREVIOUS candidate's assignment_id (same round,
        # same validator) is stale — it must not read as terminal for the
        # current assignment.
        a_now = _assignment("0xV1")
        a_prev = _assignment("0xV1")
        a_prev.candidate_image_id = "c" * 64
        phase = self._phase([a_now], deadline=100)
        phase.responses["0xv1"] = _response(a_prev)
        assert resolve_phase(phase, current_epoch=5) == (ACTION_WAIT, None)

    def test_resolved_is_noop(self):
        phase = self._phase([])
        phase.resolved = True
        assert resolve_phase(phase, current_epoch=0) == (ACTION_NOOP, None)

    def test_state_round_trips_bounded(self):
        a1 = _assignment("0xV1")
        phase = self._phase([a1])
        phase.responses["0xv1"] = _response(a1, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "catastrophic", "1000", "900"),
        ])
        phase.unsupported.append("0xdead")
        restored = VetoPhaseState.from_dict(phase.to_dict())
        assert restored == phase
        # Bounded: the persisted form carries ids/hashes/claims — never
        # per-order result rows (round_store rewrites the whole file per
        # mutation on the event loop; keep it tiny).
        blob = str(phase.to_dict())
        assert "raw_output" not in blob and "per_intent" not in blob


# ── entropy-seeded selection ─────────────────────────────────────────────────

class TestSelection:
    def test_assign_slices_deterministic_and_entropy_sensitive(self):
        evms = ["0xA1", "0xB2", "0xC3", "0xD4"]
        m1 = assign_slices(evms, 3, entropy="block_hash_1")
        m2 = assign_slices(evms, 3, entropy="block_hash_1")
        assert m1 == m2
        assert len(m1) == 3
        assert sorted(m1.values()) == [0, 1, 2]
        # different post-close entropy re-deals the mapping (over a few draws)
        assert any(
            assign_slices(evms, 3, entropy=f"block_hash_{i}") != m1
            for i in range(2, 8)
        )

    def test_assign_slices_case_insensitive_and_deduped(self):
        m = assign_slices(["0xAA", "0xaa", "0xBB"], 5, entropy="e")
        assert set(m) == {"0xaa", "0xbb"}

    def test_audit_target_round_scoped(self):
        responders = ["0xA1", "0xB2", "0xC3"]
        t1 = pick_audit_target(responders, entropy="close_hash_round_1")
        assert t1 in {e.lower() for e in responders}
        # a fixed seed would audit the SAME validator forever — different
        # rounds' entropy must be able to pick different targets
        assert any(
            pick_audit_target(responders, entropy=f"close_hash_round_{i}") != t1
            for i in range(2, 12)
        )
        assert pick_audit_target([], entropy="x") is None

    def test_audit_orders_deterministic_subset(self):
        ids = [f"ord_{i}" for i in range(20)]
        got = pick_audit_orders(ids, entropy="e1")
        assert got == pick_audit_orders(ids, entropy="e1")
        assert len(got) == 3 and set(got) <= set(ids)


# ── re-verification planning ─────────────────────────────────────────────────

class TestReverifyPlan:
    def test_dropped_first_then_largest_cut_exact_ints(self):
        a = _assignment(order_ids=[f"ord_{i}" for i in range(6)])
        violations = [
            SliceViolation("ord_0", "catastrophic", "1000", "985"),   # 1.5% cut
            SliceViolation("ord_1", "catastrophic", "1000", "900"),   # 10% cut
            SliceViolation("ord_2", "dropped", "1000", "0"),
            SliceViolation(
                "ord_3", "catastrophic",
                # huge-wei values: exact integer math, no float precision loss
                "1000000000000000000000000", "979999999999999999999999",  # 2.0001%
            ),
        ]
        r = _response(a, verdict=VERDICT_VETO, violations=violations)
        plan = plan_reverification(r)
        assert [v.order_id for v in plan] == ["ord_2", "ord_1", "ord_3", "ord_0"]

    def test_budget_caps_the_plan(self):
        ids = [f"ord_{i}" for i in range(VETO_VERIFY_BUDGET_ORDERS + 5)]
        a = _assignment(order_ids=ids)
        r = _response(a, verdict=VERDICT_VETO, violations=[
            SliceViolation(oid, "dropped", "1", "0") for oid in ids
        ])
        assert len(plan_reverification(r)) == VETO_VERIFY_BUDGET_ORDERS

    def test_duplicate_order_claims_deduped(self):
        # 10 duplicate claims on one order must not crowd the budget out of a
        # legitimate multi-order dissent.
        a = _assignment(order_ids=["ord_a", "ord_b"])
        r = _response(a, verdict=VERDICT_VETO, violations=(
            [SliceViolation("ord_a", "catastrophic", "1000", "900")] * 10
            + [SliceViolation("ord_b", "catastrophic", "1000", "980")]
        ))
        plan = plan_reverification(r)
        assert [v.order_id for v in plan] == ["ord_a", "ord_b"]

    def test_ok_response_plans_nothing(self):
        a = _assignment()
        assert plan_reverification(_response(a)) == []

    def test_malformed_wei_claims_do_not_crash(self):
        a = _assignment()
        r = _response(a, verdict=VERDICT_VETO, violations=[
            SliceViolation("ord_a", "catastrophic", "not-a-number", ""),
            SliceViolation("ord_b", "dropped", "", ""),
        ])
        plan = plan_reverification(r)
        assert [v.order_id for v in plan] == ["ord_b", "ord_a"]
