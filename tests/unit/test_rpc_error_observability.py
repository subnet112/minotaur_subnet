"""Surface + count transient RPC/provider (e.g. Alchemy) errors during benchmarking.

A solver quotes/routes against a live provider from inside its container; a
rate-limit / timeout / 5xx makes it emit NO plan for the affected order, which the
scorer records as a blind spot / drop and zeroes it — misattributing provider
flake to a lack of miner capability (a fairness bug). These tests pin the pure
classifier and the per-session counters that SURFACE + COUNT such failures.

The instrumentation is OBSERVABILITY ONLY: nothing here (or in the code it covers)
touches scoring, benchmark results, or the pack hash.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.harness.orchestrator import (  # noqa: E402
    SolverSession,
    _classify_rpc_error,
)


def _session() -> SolverSession:
    # stderr=None → the background stderr drain no-ops, so no event loop is needed
    # to construct the session in a plain sync test.
    return SolverSession(SimpleNamespace(stderr=None), label="miner-x")


# ── the pure classifier ───────────────────────────────────────────────────────


def test_classifier_matches_transient_rpc_signatures():
    for line in (
        "HTTP 429 Too Many Requests",
        "eth_call: request timeout after 10000ms",
        "Error: ECONNRESET",
        "Alchemy: exceeded your compute unit capacity",
        "upstream: 503 Service Unavailable",
        '{"code":-32005,"message":"rate limit exceeded"}',
    ):
        assert _classify_rpc_error(line) is not None, line


def test_classifier_ignores_genuine_capability_and_noise():
    # A genuine "can't serve this pair" must NOT be flagged as a transient RPC
    # error — keeping those apart is the whole point of the audit.
    for line in (
        "no route found for WETH->USDC",
        "plan generated: 1 hop via uniswap-v3",
        "scoreIntent reverted: insufficient liquidity",
        "",
    ):
        assert _classify_rpc_error(line) is None, line
    assert _classify_rpc_error(None) is None


# ── session-level counting (fairness audit) ───────────────────────────────────


def test_stderr_rpc_errors_counted_and_sampled():
    s = _session()
    s._note_stderr_line("quoting WETH->USDC")                      # normal → ignored
    s._note_stderr_line("provider error: 429 too many requests")   # rpc
    s._note_stderr_line("route ok")                                # normal → ignored
    s._note_stderr_line("fetch failed: connection reset by peer")  # rpc
    n, samples = s.rpc_error_report()
    assert n == 2
    assert any("429" in x for x in samples)
    assert any("connection reset" in x for x in samples)


def test_protocol_error_classified_and_counted():
    s = _session()
    assert s._note_protocol_rpc_error("gateway timeout contacting upstream") is not None
    assert s._note_protocol_rpc_error("invalid intent params") is None  # not RPC
    n, _ = s.rpc_error_report()
    assert n == 1  # only the transient one is counted


def test_clean_run_reports_zero():
    s = _session()
    s._note_stderr_line("all good")
    n, samples = s.rpc_error_report()
    assert n == 0
    assert samples == []
