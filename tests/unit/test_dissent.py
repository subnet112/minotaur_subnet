"""Tests for consensus dissent log."""

from __future__ import annotations

import pytest

from minotaur_subnet.consensus.dissent import (
    DissentLog,
    DissentEvent,
    RejectionCode,
    record_dissent,
    get_dissent_log,
)


def test_rejection_code_round_trips_from_string():
    """Peer wire-format speaks strings; leader must parse them back cleanly."""
    assert RejectionCode("TIMEOUT") is RejectionCode.TIMEOUT
    assert RejectionCode("BENCHMARK_MISMATCH") is RejectionCode.BENCHMARK_MISMATCH


def test_rejection_code_unknown_value_raises():
    """Unknown strings are not silently coerced; caller must fall back."""
    with pytest.raises(ValueError):
        RejectionCode("not-a-real-code")


def test_dissent_log_records_and_counts():
    log = DissentLog(capacity=10)

    log.record(DissentEvent(
        peer_id="0x" + "aa" * 20,
        code=RejectionCode.BENCHMARK_MISMATCH,
        subject_kind="round",
        subject_id="round-7",
        reason="local=0.80 leader=0.95",
    ))
    log.record(DissentEvent(
        peer_id="0x" + "aa" * 20,
        code=RejectionCode.BENCHMARK_MISMATCH,
        subject_kind="round",
        subject_id="round-8",
        reason="",
    ))
    log.record(DissentEvent(
        peer_id="0x" + "bb" * 20,
        code=RejectionCode.TIMEOUT,
        subject_kind="order",
        subject_id="ord_1",
        reason="",
    ))

    counts = log.counts()
    assert counts["BENCHMARK_MISMATCH"] == 2
    assert counts["TIMEOUT"] == 1
    assert sum(counts.values()) == 3

    recent = log.recent(limit=10)
    assert len(recent) == 3
    assert recent[-1].code is RejectionCode.TIMEOUT


def test_dissent_log_capacity_bounds_memory():
    """Ring buffer drops oldest past capacity — counts still accumulate."""
    log = DissentLog(capacity=3)
    for i in range(7):
        log.record(DissentEvent(
            peer_id="0x" + "cc" * 20,
            code=RejectionCode.SIG_INVALID,
            subject_kind="order",
            subject_id=f"ord_{i}",
            reason="",
        ))
    # Only the last 3 events remain in the buffer...
    assert len(log.recent(limit=100)) == 3
    # ... but the per-code counter has seen all 7.
    assert log.counts()["SIG_INVALID"] == 7


def test_record_dissent_helper_coerces_string_codes():
    log = get_dissent_log()
    before = log.counts().get("BENCHMARK_ERROR", 0)
    record_dissent(
        peer_id="0x" + "dd" * 20,
        code="BENCHMARK_ERROR",  # string, not enum
        subject_kind="round",
        subject_id="round-x",
        reason="docker failure",
    )
    assert log.counts()["BENCHMARK_ERROR"] == before + 1


def test_record_dissent_helper_falls_back_on_garbage():
    log = get_dissent_log()
    before = log.counts().get("UNKNOWN", 0)
    record_dissent(
        peer_id="0x" + "ee" * 20,
        code="something-weird-from-a-peer",
        subject_kind="order",
        subject_id="ord_z",
        reason="",
    )
    assert log.counts()["UNKNOWN"] == before + 1
