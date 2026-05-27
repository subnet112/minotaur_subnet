"""Tests for quorum-required reading from the on-chain validator count.

Pre-fix, ``ConsensusManager.quorum_required`` used
``len(self.validators)`` — the in-memory union of (self + env-pinned +
discovered peers). That set jitters with peer-discovery availability:
a transiently attested peer that drops on the next refresh would
briefly inflate the count, pushing quorum_required up just long enough
for an in-flight order to require one more signature than the on-chain
truth says it should. Hit live on prod 2026-05-27.

Post-fix, ``quorum_required`` reads from
``ProtocolConfig.on_chain_validator_count`` — the canonical denominator
that the on-chain ``EIP712Verifier`` uses. The discovery layer
determines WHO we broadcast to; the on-chain count determines HOW MANY
signatures are required.

These tests pin:
  - quorum_required reflects on-chain count, not in-memory peer set
  - peer-discovery jitter (briefly attesting an extra peer) does NOT
    affect quorum_required
  - peers dropping out (in-memory count < on-chain count) also does
    NOT lower quorum_required — that's the protection against split-set
    minorities approving orders
  - legacy fallback to len(validators) when on_chain_validator_count
    is unset (0) — preserves behaviour for tests that build
    ProtocolConfig manually
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.protocol_config import ProtocolConfig


def _make_protocol_config(*, quorum_bps=6666, on_chain_count=6, peers=None):
    """Build a ProtocolConfig with the fields ConsensusManager reads."""
    return ProtocolConfig(
        quorum_bps=quorum_bps,
        rpc_url="http://anvil:8545",
        registry_address="0x" + "11" * 20,
        on_chain_validator_count=on_chain_count,
        peers=peers or [],
    )


def _make_manager(protocol_config, *, validators=None):
    """Build a ConsensusManager with the given ProtocolConfig.

    Uses a deterministic private key (Anvil account #0) so signing
    machinery works if any test ends up exercising it.
    """
    return ConsensusManager(
        validator_id="0x" + "aa" * 20,
        private_key="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
        protocol_config=protocol_config,
        validators=validators,
        chain_id=8453,
    )


# ── Happy path: on-chain count is the denominator ──────────────────────


def test_quorum_required_uses_on_chain_count():
    """6 chain validators, 66.66% bps → quorum 4. Matches the live prod
    state at the time of the order-failure incident."""
    pc = _make_protocol_config(quorum_bps=6666, on_chain_count=6)
    mgr = _make_manager(pc)
    assert mgr.quorum_required == 4


def test_quorum_required_ignores_peer_inflation():
    """The exact bug we shipped this PR to fix: discovery briefly attests
    one extra peer (6 in-memory peers + self = 7 in-memory validators),
    but on-chain count is still 6. Pre-fix, quorum would jump to 5 and
    in-flight orders with 4 valid signatures would fail. Post-fix,
    quorum stays at 4 — the chain truth."""
    inflated_peers = [
        MagicMock(evm_address=f"0x{i:02x}{'00'*19}") for i in range(1, 7)  # 6 peers
    ]
    pc = _make_protocol_config(
        quorum_bps=6666,
        on_chain_count=6,
        peers=inflated_peers,
    )
    mgr = _make_manager(pc)
    # In-memory count says 7 (1 self + 6 peers); on-chain says 6.
    assert len(mgr.validators) == 7
    # Quorum reads from chain, not in-memory.
    assert mgr.quorum_required == 4


def test_quorum_required_unaffected_by_peer_dropout():
    """Inverse jitter: a peer briefly drops, in-memory count is 5
    (self + 4 attested), but the on-chain registry still has 6. The
    chain truth is what binds the on-chain verifier on submit, so
    quorum_required stays at 4 — preventing a degraded-network split
    from lowering quorum into the minority-approval danger zone."""
    sparse_peers = [
        MagicMock(evm_address=f"0x{i:02x}{'00'*19}") for i in range(1, 5)  # 4 peers
    ]
    pc = _make_protocol_config(
        quorum_bps=6666,
        on_chain_count=6,
        peers=sparse_peers,
    )
    mgr = _make_manager(pc)
    assert len(mgr.validators) == 5  # in-memory says 5
    assert mgr.quorum_required == 4  # chain says quorum is still 4


# ── Edge cases ─────────────────────────────────────────────────────────


def test_quorum_required_minimum_one():
    """Even with on_chain_count=0 (unconfigured) AND zero peers, quorum
    is at least 1 (the leader itself). The ``max(1, ...)`` guard prevents
    a zero quorum that would auto-pass every order."""
    pc = _make_protocol_config(quorum_bps=6666, on_chain_count=0)
    mgr = _make_manager(pc)
    # Legacy fallback: in-memory count is 1 (just self), quorum = 1.
    assert mgr.quorum_required == 1


def test_quorum_required_falls_back_to_in_memory_when_unconfigured():
    """If on_chain_validator_count is 0 (e.g. older test fixture that
    constructed ProtocolConfig directly without
    ``from_validator_registry``), quorum_required falls back to
    ``len(self.validators)``. Preserves behaviour for the existing test
    suite that doesn't wire on-chain reads."""
    peers = [
        MagicMock(evm_address=f"0x{i:02x}{'00'*19}") for i in range(1, 5)  # 4 peers
    ]
    pc = _make_protocol_config(
        quorum_bps=6666,
        on_chain_count=0,  # ← unconfigured
        peers=peers,
    )
    mgr = _make_manager(pc)
    # Falls back: in-memory count = 1 self + 4 peers = 5; quorum = ceil(5 * 0.6666) = 4.
    assert mgr.quorum_required == 4


def test_quorum_required_high_bps():
    """Sanity: 7 validators at 8000 bps → quorum 6 (= ceil(7 * 0.8))."""
    pc = _make_protocol_config(quorum_bps=8000, on_chain_count=7)
    mgr = _make_manager(pc)
    assert mgr.quorum_required == 6


def test_quorum_required_ceil_division_boundary():
    """6 validators × 6666 bps = 39996, ceil_div by 10000 → 4 (not 3).
    Matches the on-chain ``EIP712Verifier``'s integer-math contract
    (``(n * quorumBps + 9999) / 10000``), so off-chain quorum agrees
    with the on-chain verifier byte-for-byte."""
    pc = _make_protocol_config(quorum_bps=6666, on_chain_count=6)
    mgr = _make_manager(pc)
    assert mgr.quorum_required == 4

    # Another boundary: 3 × 6666 = 19998 → ceil → 2.
    pc.on_chain_validator_count = 3
    assert mgr.quorum_required == 2


def test_protocol_config_default_zero():
    """Default-constructed ProtocolConfig has on_chain_validator_count=0
    so existing callers that construct ProtocolConfig manually (without
    from_validator_registry) get the legacy fallback path."""
    pc = ProtocolConfig(
        quorum_bps=6666,
        rpc_url="http://anvil:8545",
        registry_address="0x" + "11" * 20,
    )
    assert pc.on_chain_validator_count == 0
