"""Fleet determinism-parity poller (scripts/check_determinism_parity.py).

The poller gates the p2oc flip: it sweeps every validator's /health round-anchor
probe and only reports PARITY when all nodes agree, per anchor_epoch, on both the
fork pin AND the block hash at that pin. A false AGREE could green-light a bad
flip; a false DIVERGE could block a safe one — so the diff logic is pinned here.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import check_determinism_parity as C  # noqa: E402


def _p(url, epoch, pins, hashes, ok=True):
    return {
        "url": url, "ok": ok, "anchor_epoch": epoch, "status": "ok",
        "pins": pins, "pin_hashes": hashes,
    }


# ── diff / verdict ──────────────────────────────────────────────────────────


def test_agree_when_pins_and_hashes_match():
    probes = [
        _p("a", 1, {"8453": 7}, {"8453": "abc"}),
        _p("b", 1, {"8453": 7}, {"8453": "abc"}),
    ]
    _, ok = C.summarize(probes)
    assert ok is True


def test_diverge_on_hash_mismatch():
    probes = [
        _p("a", 1, {"8453": 7}, {"8453": "abc"}),
        _p("b", 1, {"8453": 7}, {"8453": "dead"}),
    ]
    _, ok = C.summarize(probes)
    assert ok is False


def test_diverge_on_pin_mismatch():
    probes = [
        _p("a", 1, {"8453": 7}, {"8453": "abc"}),
        _p("b", 1, {"8453": 9}, {"8453": "abc"}),
    ]
    _, ok = C.summarize(probes)
    assert ok is False


def test_unreachable_only_is_not_parity():
    _, ok = C.summarize([{"url": "x", "ok": False, "error": "timeout"}])
    assert ok is False


def test_different_epochs_compared_within_group_only():
    # Nodes at different anchor_epochs aren't compared to each other; each group
    # is internally consistent → parity holds.
    probes = [
        _p("a", 1, {"8453": 7}, {"8453": "abc"}),
        _p("b", 2, {"8453": 8}, {"8453": "def"}),
    ]
    _, ok = C.summarize(probes)
    assert ok is True


# ── hash normalization (cross web3-version robustness) ──────────────────────


def test_hash_normalization_0x_and_case():
    probes = [
        _p("a", 1, {"8453": 7}, {"8453": "0xABC"}),
        _p("b", 1, {"8453": 7}, {"8453": "abc"}),
    ]
    _, ok = C.summarize(probes)
    assert ok is True, "0x-prefix / case must not masquerade as divergence"


# ── reuse of consensus peer discovery ───────────────────────────────────────


def test_swap_port():
    assert C._swap_port("http://1.2.3.4:9100", 8080) == "http://1.2.3.4:8080"
    assert C._swap_port("1.2.3.4:9100", 8080) == "http://1.2.3.4:8080"


def test_fleet_from_leader_reuses_peer_endpoints(monkeypatch):
    payload = {
        "champion_consensus": {
            "peer_endpoints": [
                {"validator_id": "0xAAA", "url": "http://10.0.0.2:9100"},
                {"validator_id": "0xBBB", "url": "http://10.0.0.3:9100"},
            ]
        }
    }

    class _Resp:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

        def read(self_):
            return json.dumps(payload).encode()

    monkeypatch.setattr(C.urllib.request, "urlopen", lambda u, timeout=10: _Resp())
    fleet = C.fleet_from_leader("http://10.0.0.1:8080", 8080)
    # leader + both peers, normalized to the api port, deduped, order-preserving.
    assert fleet == [
        "http://10.0.0.1:8080",
        "http://10.0.0.2:8080",
        "http://10.0.0.3:8080",
    ]
