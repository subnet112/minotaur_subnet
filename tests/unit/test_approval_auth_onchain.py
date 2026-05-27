"""Tests for the approval-authorization check reading from the on-chain registry.

Pre-fix, ``ConsensusManager._receive_approval`` rejected incoming
approvals when ``approval.validator_id`` wasn't in ``self.validators``
— the in-memory union of (self + env-pinned + discovered peers). The
discovered set drops peers transiently when their /identity probe
times out mid-refresh, so a chain-authorized peer that returned a
valid signature could be rejected as "non-validator" if its /identity
probe happened to fail in between propose() and the approval arrival.

Hit live on prod 2026-05-27: discovery went 5→3 mid-order, two
valid signatures arrived from peers that had just dropped from the
in-memory set, both got rejected, order failed with collected=3
quorum=4.

Post-fix, authorization reads from
``ProtocolConfig.on_chain_validators`` — the canonical list from
``ValidatorRegistry.getValidators()`` which only changes when the
owner calls ``updateValidators``. Network reachability no longer
affects authorization.
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


# Anvil's well-known accounts so signing machinery has real keys if needed.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
LEADER_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # account 0


def _make_protocol_config(
    *, on_chain_validators=None, peers=None, on_chain_count=None,
):
    """Build a ProtocolConfig with chain + discovery state both controllable."""
    if on_chain_count is None:
        on_chain_count = len(on_chain_validators) if on_chain_validators else 0
    return ProtocolConfig(
        quorum_bps=6666,
        rpc_url="http://anvil:8545",
        registry_address="0x" + "11" * 20,
        on_chain_validator_count=on_chain_count,
        on_chain_validators=on_chain_validators or [],
        peers=peers or [],
    )


def _make_manager(protocol_config, *, validators=None):
    return ConsensusManager(
        validator_id=LEADER_ADDR,
        private_key=TEST_KEY,
        protocol_config=protocol_config,
        validators=validators,
        chain_id=8453,
    )


# ── On-chain authorization wins ────────────────────────────────────────


def test_signer_on_chain_authorized_even_when_not_in_peers():
    """The fix in one assertion: a peer on the chain registry IS authorized
    to sign, even if peer-discovery has dropped them from ``peers`` due
    to a transient /identity failure. This was the live failure mode."""
    chain_validator = "0xbe93685473ce8fb096997394ea11f7ede92a0ae9"  # from prod
    pc = _make_protocol_config(
        on_chain_validators=[LEADER_ADDR, chain_validator],
        peers=[],  # ← peer discovery dropped them
    )
    mgr = _make_manager(pc)

    # in-memory union doesn't include them (legacy check would reject)
    assert chain_validator.lower() not in {v.lower() for v in mgr.validators}
    # on-chain auth accepts them
    assert mgr._is_authorized_signer(chain_validator) is True


def test_signer_not_on_chain_rejected():
    """A signer that isn't on the chain registry must still be rejected
    — authorization is anchored to chain truth, NOT to whoever happens
    to send us an approval."""
    chain_validator = "0xbe93685473ce8fb096997394ea11f7ede92a0ae9"
    pc = _make_protocol_config(
        on_chain_validators=[LEADER_ADDR, chain_validator],
        peers=[],
    )
    mgr = _make_manager(pc)

    impostor = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert mgr._is_authorized_signer(impostor) is False


def test_signer_in_peers_but_not_on_chain_rejected():
    """Defense in depth: even if peer-discovery somehow returns a peer
    that's NOT on chain, authorization must still reject. (The discovery
    layer already filters by chain-authorization, but this property
    ensures the auth check doesn't trust discovery alone.)"""
    chain_validator = "0xbe93685473ce8fb096997394ea11f7ede92a0ae9"
    phantom = MagicMock(evm_address="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    pc = _make_protocol_config(
        on_chain_validators=[LEADER_ADDR, chain_validator],
        peers=[phantom],
    )
    mgr = _make_manager(pc)

    assert mgr._is_authorized_signer(phantom.evm_address) is False


def test_case_insensitive_match():
    """EVM addresses come in checksummed and lowercase forms; the
    authorization match must be case-insensitive so a peer attesting
    with checksum and signing with lowercase still matches."""
    chain_validator_checksummed = "0xBe93685473cE8fB096997394eA11F7EDE92A0AE9"
    chain_validator_lowercase = chain_validator_checksummed.lower()
    pc = _make_protocol_config(
        on_chain_validators=[LEADER_ADDR, chain_validator_checksummed],
        peers=[],
    )
    mgr = _make_manager(pc)

    # Lowercase incoming signature still authorized.
    assert mgr._is_authorized_signer(chain_validator_lowercase) is True
    # And the reverse direction.
    pc.on_chain_validators = [LEADER_ADDR, chain_validator_lowercase]
    assert mgr._is_authorized_signer(chain_validator_checksummed) is True


# ── Legacy fallback when on-chain list is empty ────────────────────────


def test_fallback_to_in_memory_when_on_chain_empty():
    """Tests + local-testnet that build ProtocolConfig directly (without
    ``from_validator_registry``) leave ``on_chain_validators`` empty.
    The auth check falls back to the in-memory union, preserving the
    existing test contract."""
    legacy_peer = "0x000000000000000000000000000000000000abcd"
    pc = _make_protocol_config(
        on_chain_validators=[],  # ← unconfigured
        peers=[],
    )
    mgr = _make_manager(pc, validators=[legacy_peer])

    # legacy_peer is in the in-memory union (env-pinned override)
    assert legacy_peer.lower() in {v.lower() for v in mgr.validators}
    # Auth check accepts via fallback
    assert mgr._is_authorized_signer(legacy_peer) is True


def test_fallback_rejects_when_not_in_in_memory():
    """Symmetric to the above: fallback path still rejects truly
    unknown addresses."""
    pc = _make_protocol_config(on_chain_validators=[], peers=[])
    mgr = _make_manager(pc)

    assert mgr._is_authorized_signer("0x" + "ff" * 20) is False


# ── Live failure mode reproduction ─────────────────────────────────────


def test_repro_prod_2026_05_27_peer_dropped_midorder():
    """Reproduces the live failure: order propose() captures 5 peers,
    discovery drops 2 of them seconds later, those 2 still return valid
    signatures. Pre-fix: rejected. Post-fix: accepted because they're
    still on chain."""
    # Order-time peer set (5 chain-authorized peers + self = 6 validators)
    chain_validators = [
        LEADER_ADDR,
        "0x19235203853dd4a8dBc7C717EC669C9391E16aa1",
        "0xBe93685473cE8fB096997394eA11F7EDE92A0AE9",
        "0x8F0baC1081661E193C21028dD1dd1002cD962d9A",
        "0x8D5aBa035D54128AD4d5380866AF8bf33BFB6Bd7",
        "0x7EF6fAFCd590AD9F60fDa6dE093DbD238F3845b7",
    ]
    # Post-jitter: only 3 peers still attested (self + 2 others)
    surviving_peers = [
        MagicMock(evm_address="0x8F0baC1081661E193C21028dD1dd1002cD962d9A"),
        MagicMock(evm_address="0x7EF6fAFCd590AD9F60fDa6dE093DbD238F3845b7"),
    ]
    pc = _make_protocol_config(
        on_chain_validators=chain_validators,
        peers=surviving_peers,
    )
    mgr = _make_manager(pc)

    # The two peers that DID NOT survive the discovery jitter — but
    # ARE on chain. These were the ones whose sigs got rejected live.
    dropped_signers = [
        "0x19235203853dd4a8dBc7C717EC669C9391E16aa1",
        "0xBe93685473cE8fB096997394eA11F7EDE92A0AE9",
    ]
    for signer in dropped_signers:
        # Pre-fix the in-memory check would say False here.
        assert signer.lower() not in {v.lower() for v in mgr.validators}
        # Post-fix the on-chain check says True — they're still authorized.
        assert mgr._is_authorized_signer(signer) is True


# ── Champion-consensus parity ──────────────────────────────────────────


def test_champion_consensus_uses_same_auth_path():
    """ChampionConsensusManager has the same architectural pattern and
    same fix. Smoke test that its ``_is_authorized_signer`` reads from
    on-chain when wired."""
    from minotaur_subnet.consensus.champion_manager import ChampionConsensusManager

    chain_validator = "0xbe93685473ce8fb096997394ea11f7ede92a0ae9"
    pc = _make_protocol_config(
        on_chain_validators=[LEADER_ADDR, chain_validator],
        peers=[],
    )
    mgr = ChampionConsensusManager(
        validator_id=LEADER_ADDR,
        private_key=TEST_KEY,
        protocol_config=pc,
    )
    assert mgr._is_authorized_signer(chain_validator) is True
    assert mgr._is_authorized_signer("0x" + "ff" * 20) is False


def test_protocol_config_default_empty():
    """Default-constructed ProtocolConfig has empty on_chain_validators
    so existing callers fall back to the legacy path."""
    pc = ProtocolConfig(
        quorum_bps=6666,
        rpc_url="http://anvil:8545",
        registry_address="0x" + "11" * 20,
    )
    assert pc.on_chain_validators == []
