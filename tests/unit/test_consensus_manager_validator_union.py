"""Regression tests for the ``ConsensusManager.validators`` union behavior.

Before 2026-05-26: ``validators=...`` ctor arg was a mutually-exclusive
override. A leader pinned with ``validators=[A, B, C]`` would treat any
signature from a peer not in that list — including discovered third-party
validators — as "from non-validator" and reject it in ``receive_approval``.
Combined with the relayer reading the on-chain ``ValidatorRegistry`` for
its own quorum count, third-party validators registered on-chain but
unreachable through env caused the leader to undercollect: bundle goes out
with ≤ env count sigs, relayer requires ≥ ceil(N_onchain * quorum_bps),
order rejected.

After: ``validators=...`` is an additive trust source unioned with
``protocol_config.peers``. In-cluster peers (no metagraph hotkey) stay
env-pinned; third-party validators get added via discovery without
restart. Mirrors the existing union in ``ValidatorPeerNetwork.peers``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from eth_account import Account

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.consensus.eip712 import (
    address_from_key,
    build_domain_separator,
)
from minotaur_subnet.consensus.manager import ConsensusManager
from minotaur_subnet.consensus.peer_discovery import PeerInfo
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.consensus.signatures import hash_plan, sign_plan_approval
from minotaur_subnet.shared.types import ExecutionPlan, Interaction


# Anvil deterministic keys 0-5 — stable across machines, no leaked secrets.
KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
]
ADDRS = [address_from_key(k) for k in KEYS]


def _cfg(quorum_bps: int = 6666, peers: list[PeerInfo] | None = None) -> ProtocolConfig:
    return ProtocolConfig(
        quorum_bps=quorum_bps,
        rpc_url="",
        registry_address="",
        peers=list(peers or []),
    )


def _peer(addr: str, suffix: int) -> PeerInfo:
    return PeerInfo(
        evm_address=addr,
        hotkey=f"5Hotkey{suffix}",
        axon_url=f"http://peer-{suffix}:9100",
    )


# ── validators property: union semantics ──────────────────────────────────


def test_validators_union_self_plus_env_plus_discovered():
    """The canonical real-world shape: env-pinned in-cluster peers +
    discovered third-party peers + self. All three sources contribute."""
    discovered = [_peer(ADDRS[3], 3), _peer(ADDRS[4], 4)]
    cfg = _cfg(peers=discovered)
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        validators=[ADDRS[1], ADDRS[2]],  # in-cluster, no metagraph presence
    )
    # Self first, then env-pinned, then discovered.
    assert cm.validators == [ADDRS[0], ADDRS[1], ADDRS[2], ADDRS[3], ADDRS[4]]


def test_validators_dedupe_self_appears_in_env():
    """When the env-pinned list contains the leader's own address, the
    union does not double-count it."""
    cfg = _cfg(peers=[_peer(ADDRS[2], 2)])
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        validators=[ADDRS[0], ADDRS[1]],  # self redundantly listed
    )
    assert cm.validators == [ADDRS[0], ADDRS[1], ADDRS[2]]


def test_validators_dedupe_env_and_discovered_overlap():
    """If a peer appears in both env-pinned and discovered (e.g. an
    operator who runs in-cluster but also registers on-chain), the union
    keeps the env-pinned entry (first-seen)."""
    cfg = _cfg(peers=[_peer(ADDRS[1], 1)])  # discovered duplicates env
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        validators=[ADDRS[1]],
    )
    assert cm.validators == [ADDRS[0], ADDRS[1]]


def test_validators_dedupe_is_case_insensitive():
    """EVM addresses can appear in different cases across sources
    (lowercase in env vs checksummed from discovery). Dedup must collapse
    them."""
    addr_checksum = ADDRS[1]
    addr_lower = addr_checksum.lower()
    cfg = _cfg(peers=[_peer(addr_checksum, 1)])
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        validators=[addr_lower],  # different case than discovered
    )
    # Exactly 2 entries — self + one (any case wins, but only one).
    assert len(cm.validators) == 2
    assert cm.validators[0] == ADDRS[0]
    # The env-pinned (lowercased) form wins on conflict per first-seen rule.
    assert cm.validators[1].lower() == addr_lower


# ── quorum scales with the unioned set ────────────────────────────────────


def test_quorum_required_includes_discovered_peers():
    """Quorum denominator follows the unioned validator count. As
    discovery picks up new peers, the threshold scales."""
    cfg = _cfg(quorum_bps=6666, peers=[])
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        validators=[ADDRS[1], ADDRS[2]],  # 1 leader + 2 in-cluster
    )
    # 3 validators × 66.66% → ceil(2) = 2
    assert cm.quorum_required == 2

    # Discovery loop picks up two third-party validators.
    cfg.peers[:] = [_peer(ADDRS[3], 3), _peer(ADDRS[4], 4)]
    # 5 validators × 66.66% → ceil(3.333) = 4
    assert cm.quorum_required == 4

    # Another third party joins.
    cfg.peers.append(_peer(ADDRS[5], 5))
    # 6 validators × 66.66% → ceil(3.9996) = 4
    assert cm.quorum_required == 4


# ── receive_approval accepts approvals from discovered peers ──────────────


def _signed_approval(
    *,
    signer_key: str,
    signer_addr: str,
    order_id: str,
    plan_hash: str,
    score: float,
    score_bps: int,
):
    """Build a SignedApproval matching what a peer would return."""
    from minotaur_subnet.shared.types import SignedApproval

    sig_hex = sign_plan_approval(
        signer_key,
        order_id,
        plan_hash,
        score,
        domain_separator=build_domain_separator(31337, "0x" + "00" * 20),
        score_bps=score_bps,
    )
    return SignedApproval(
        validator_id=signer_addr,
        order_id=order_id,
        plan_hash=plan_hash,
        score=score,
        signature=sig_hex,
        timestamp=0.0,
    )


@pytest.mark.asyncio
async def test_receive_approval_accepts_discovered_peer():
    """A signature from a peer that is ONLY in ``protocol_config.peers``
    (not in env-pinned) must be accepted post-union. Pre-fix it would have
    been dropped with "Received approval from non-validator"."""
    plan = ExecutionPlan(
        intent_id="x",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0xdeadbeef",
                chain_id=31337,
            ),
        ],
        deadline=10_000_000_000,
        nonce=0,
        metadata={},
    )
    plan_hash = hash_plan(plan)
    order_id = "ord_x"

    # Leader runs with NO env-pinned peers, only one discovered peer.
    # Quorum bps chosen so 1-of-2 is NOT enough (must wait for the peer):
    # ceil(2 * 6666 / 10000) = 2, so propose() blocks until the peer's
    # approval arrives via receive_approval — which is what we're testing.
    discovered_peer_addr = ADDRS[1]
    discovered_peer_key = KEYS[1]
    cfg = _cfg(quorum_bps=6666, peers=[_peer(discovered_peer_addr, 1)])
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        chain_id=31337,
        contract_address="0x" + "00" * 20,
        score_threshold_bps=5000,
    )

    # Leader proposes (signs its own approval first).
    propose_task = pytest.importorskip("asyncio").ensure_future(
        cm.propose(order_id, plan, 0.85, plan_hash)
    )
    # Discovered peer "responds" with its signature.
    approval = _signed_approval(
        signer_key=discovered_peer_key,
        signer_addr=discovered_peer_addr,
        order_id=order_id,
        plan_hash=plan_hash,
        score=0.85,
        score_bps=5000,
    )
    # Give propose() a moment to register the pending proposal first.
    import asyncio
    await asyncio.sleep(0.01)
    result_from_recv = await cm.receive_approval(approval)
    assert result_from_recv is not None, (
        "discovered-peer approval was rejected by receive_approval — the "
        "union of validators failed to include it"
    )
    assert result_from_recv.reached is True

    # propose() should also see quorum and complete.
    final = await propose_task
    assert final.reached is True
    signers = {a.validator_id.lower() for a in final.approvals}
    assert ADDRS[0].lower() in signers
    assert discovered_peer_addr.lower() in signers


@pytest.mark.asyncio
async def test_receive_approval_rejects_truly_unknown_signer():
    """Negative case: a signer that is neither in env-pinned NOR in
    discovered peers must still be rejected as non-validator."""
    plan = ExecutionPlan(
        intent_id="x",
        interactions=[
            Interaction(
                target="0x" + "11" * 20,
                value="0",
                call_data="0xdeadbeef",
                chain_id=31337,
            ),
        ],
        deadline=10_000_000_000,
        nonce=0,
        metadata={},
    )
    plan_hash = hash_plan(plan)
    order_id = "ord_y"

    # Same quorum shape as the positive case — leader must wait for a
    # second sig, so the proposal stays in ``_pending`` long enough for us
    # to feed receive_approval a rogue sig and see it get dropped.
    cfg = _cfg(quorum_bps=6666, peers=[_peer(ADDRS[1], 1)])
    cm = ConsensusManager(
        validator_id=ADDRS[0],
        private_key=KEYS[0],
        protocol_config=cfg,
        chain_id=31337,
        score_threshold_bps=5000,
    )

    import asyncio
    propose_task = asyncio.ensure_future(
        cm.propose(order_id, plan, 0.85, plan_hash)
    )
    await asyncio.sleep(0.01)

    # ADDRS[2] is neither leader, env-pinned, nor in protocol_config.peers.
    rogue = _signed_approval(
        signer_key=KEYS[2],
        signer_addr=ADDRS[2],
        order_id=order_id,
        plan_hash=plan_hash,
        score=0.85,
        score_bps=5000,
    )
    result = await cm.receive_approval(rogue)
    # Rogue rejected → returns None and does NOT advance quorum.
    assert result is None

    # Cancel the propose task (timeout would otherwise hold the test).
    propose_task.cancel()
    try:
        await propose_task
    except asyncio.CancelledError:
        pass
