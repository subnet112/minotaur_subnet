"""Tests for the miner client's gated-endpoint signing and status rendering."""

from __future__ import annotations

import sys
import types

import pytest

from minotaur_subnet.miner.signing import build_canonical_message, signed_headers
from minotaur_subnet.miner.main import render_status


# ── signing ──────────────────────────────────────────────────────────────────


def test_canonical_message_matches_server_format():
    # Server rebuilds `f"{request.method} {request.url.path} {timestamp}"`.
    assert build_canonical_message("post", "/v1/apps/a1/score", 1700) == (
        "POST /v1/apps/a1/score 1700"
    )


def _install_fake_wallet(monkeypatch, captured: dict):
    class _FakeKeypair:
        ss58_address = "5FakeHotkeyAddress"

        def sign(self, message):
            captured["message"] = message
            return b"\xde\xad\xbe\xef"

    class _FakeWallet:
        def __init__(self, **kwargs):
            captured["wallet_kwargs"] = kwargs

        def get_hotkey(self):
            return _FakeKeypair()

    mod = types.ModuleType("bittensor_wallet")
    mod.Wallet = _FakeWallet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor_wallet", mod)


def test_signed_headers_builds_bittensor_headers(monkeypatch):
    captured: dict = {}
    _install_fake_wallet(monkeypatch, captured)

    headers = signed_headers(
        "POST", "/v1/apps/a1/score",
        wallet_name="w", hotkey_name="hk", timestamp=1700,
    )

    assert headers["X-Bittensor-Hotkey"] == "5FakeHotkeyAddress"
    assert headers["X-Bittensor-Signature"] == "0xdeadbeef"
    assert headers["X-Bittensor-Timestamp"] == "1700"
    # Signed over the exact canonical message the server verifies.
    assert captured["message"] == "POST /v1/apps/a1/score 1700"
    assert captured["wallet_kwargs"]["name"] == "w"
    assert captured["wallet_kwargs"]["hotkey"] == "hk"


def test_signed_headers_soft_returns_empty_when_no_wallet(monkeypatch, tmp_path):
    # required=False (the agent self-test path): no wallet → {} (unsigned),
    # never a raise, so callers can do `headers=signed_headers(...) or None`.
    monkeypatch.setenv("BT_WALLET_PATH", str(tmp_path))
    monkeypatch.delenv("MINER_WALLET_NAME", raising=False)
    monkeypatch.delenv("MINER_HOTKEY", raising=False)
    assert signed_headers("POST", "/v1/apps/a1/score", required=False) == {}


def test_signed_headers_required_raises_when_no_wallet(monkeypatch, tmp_path):
    # required=True (the CLI dry-run path): a miner asked to sign — fail loudly.
    monkeypatch.setenv("BT_WALLET_PATH", str(tmp_path))
    monkeypatch.delenv("MINER_WALLET_NAME", raising=False)
    monkeypatch.delenv("MINER_HOTKEY", raising=False)
    with pytest.raises(Exception):
        signed_headers("POST", "/v1/apps/a1/score", required=True)


# ── status rendering ─────────────────────────────────────────────────────────


def _status_with_per_order() -> dict:
    return {
        "submission_id": "sub_1",
        "status": "scored",
        "benchmark_rank": 2,
        "report": {
            "outcome": "regressed",
            "reason_relative": "1 hard loss",
            "relative": {
                "better": 1, "worse": 1, "matched": 1, "new": 0, "compared": 3,
                "verdict": "worse",
                "per_order": [
                    {"intent_id": "o_win", "champ": "100", "chal": "110",
                     "ratio": 1.1, "verdict": "win", "catastrophic": False},
                    {"intent_id": "o_drop", "champ": "50", "chal": None,
                     "ratio": None, "verdict": "dropped", "catastrophic": False},
                    {"intent_id": "o_reg", "champ": "200", "chal": "180",
                     "ratio": 0.9, "verdict": "regression", "catastrophic": True},
                    {"intent_id": "o_blind", "champ": None, "chal": "5",
                     "ratio": None, "verdict": "blind_spot_cover",
                     "catastrophic": False},
                ],
            },
        },
    }


def test_render_status_lists_per_order_actionable_first():
    out = render_status(_status_with_per_order())

    assert "Submission sub_1: scored" in out
    assert "Outcome: regressed" in out
    assert "Benchmark rank: 2" in out
    assert "Per-order verdicts (4)" in out
    # dropped sorts before regression before blind_spot_cover before win.
    order = [out.index(v) for v in ("dropped", "regression", "blind_spot_cover", "win")]
    assert order == sorted(order)
    # Catastrophic regression is flagged.
    assert "HARD-LOSS" in out
    assert "Legend:" in out


def test_render_status_without_report_is_compact():
    out = render_status({"submission_id": "sub_2", "status": "queued"})
    assert out == "Submission sub_2: queued"
