"""PR-6 consensus-hardening regression tests (audit H1, H2, H8, H9).

These document the expected behaviour of the new defences. They are
deliberately tightly scoped — full integration coverage lives in the
e2e consensus tests, which spin up a real validator process.

Marked xfail where the surrounding scaffolding (ConsensusManager,
PeerNetwork) makes a pure-unit assertion impractical; in those cases the
test still documents intent and serves as a hook for follow-up work.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from eth_account import Account
from eth_account.messages import encode_defunct

from minotaur_subnet.consensus.peer_network import (
    CHAMPION_PROPOSAL_DOMAIN_PREFIX,
    PROPOSAL_DOMAIN_PREFIX,
)
from minotaur_subnet.validator.scoring_engine import (
    ScoringEngine,
    _SEEN_PROPOSALS,
    _SEEN_PROPOSALS_LOCK,
)


# ── Test fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def signer_account():
    return Account.create()


@pytest.fixture
def scoring_engine(signer_account):
    """Build a ScoringEngine with the signer registered as a known peer."""
    js_engine = MagicMock()
    js_engine.list_loaded_intents.return_value = []
    store = MagicMock()
    store.get_deployment.return_value = None
    store.get_app.return_value = None

    consensus = MagicMock()
    consensus.validators = [signer_account.address]
    peer_network = MagicMock()
    peer_network.peers = []

    return ScoringEngine(
        js_engine=js_engine,
        store=store,
        simulator=None,
        consensus=consensus,
        peer_network=peer_network,
        validator_id=signer_account.address,
    )


def _sign_payload(payload: dict, account, *, domain: bytes = PROPOSAL_DOMAIN_PREFIX) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    message = domain + canonical.encode()
    signed = Account.sign_message(encode_defunct(message), private_key=account.key)
    return signed.signature.hex()


# ── H1: timestamp freshness ────────────────────────────────────────────


def test_stale_timestamp_rejected(scoring_engine, signer_account):
    body = {
        "order_id": "ord_test_stale",
        "plan_hash": "0xabc",
        "timestamp": time.time() - 3600,  # 1 hour old
    }
    body["proposer_signature"] = _sign_payload(body, signer_account)

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert not ok
    assert "stale_or_future_timestamp" in reason


def test_future_timestamp_rejected(scoring_engine, signer_account):
    body = {
        "order_id": "ord_test_future",
        "plan_hash": "0xabc",
        "timestamp": time.time() + 600,  # 10 min in the future
    }
    body["proposer_signature"] = _sign_payload(body, signer_account)

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert not ok
    assert "stale_or_future_timestamp" in reason


def test_missing_timestamp_rejected(scoring_engine, signer_account):
    body = {"order_id": "ord_test_none", "plan_hash": "0xabc"}
    body["proposer_signature"] = _sign_payload(body, signer_account)

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert not ok
    assert reason == "missing_timestamp"


def test_fresh_timestamp_accepted(scoring_engine, signer_account):
    body = {
        "order_id": "ord_test_ok",
        "plan_hash": "0xabc",
        "timestamp": time.time(),
    }
    body["proposer_signature"] = _sign_payload(body, signer_account)

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert ok, reason


# ── H1: replay dedup ───────────────────────────────────────────────────


def test_duplicate_proposal_short_circuits(scoring_engine):
    """A second proposal with the same (order_id, plan_hash) is rejected
    even before re-simulation."""

    async def _run():
        # Pre-populate the seen-cache as if we had already approved this
        # exact proposal.
        async with _SEEN_PROPOSALS_LOCK:
            _SEEN_PROPOSALS[("ord_dup", "0xdeadbeef")] = time.monotonic()

        body = {
            "order_id": "ord_dup",
            "plan_hash": "0xdeadbeef",
            "score": 0.99,
            "app_id": "app_test",
            "chain_id": 1,
        }
        result = await scoring_engine.verify_and_score_proposal(
            body, score_threshold=0.5,
        )
        assert not result["approved"]
        assert result["reason"] == "duplicate_proposal"

    asyncio.run(_run())


# ── H2: domain separator ───────────────────────────────────────────────


def test_domain_separator_mismatch_recovers_wrong_address(scoring_engine, signer_account):
    """A sig minted under the CHAMPION domain must NOT verify as a valid
    ORDER-consensus proposal — recovery returns a different address that
    isn't in the known-validator set, so verify_proposer_signature
    rejects."""
    body = {
        "order_id": "ord_domain_test",
        "plan_hash": "0xabc",
        "timestamp": time.time(),
    }
    # Sign with the CHAMPION prefix — wrong domain for /consensus/proposal
    body["proposer_signature"] = _sign_payload(
        body, signer_account, domain=CHAMPION_PROPOSAL_DOMAIN_PREFIX,
    )

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert not ok
    # The recovered address won't be in the known-validator set
    assert "not a registered validator" in reason or "is not the locked leader" in reason


def test_domain_separator_round_trip(scoring_engine, signer_account):
    """Sig minted under the ORDER domain must verify cleanly."""
    body = {
        "order_id": "ord_roundtrip",
        "plan_hash": "0xabc",
        "timestamp": time.time(),
    }
    body["proposer_signature"] = _sign_payload(
        body, signer_account, domain=PROPOSAL_DOMAIN_PREFIX,
    )

    ok, reason = scoring_engine.verify_proposer_signature(body)
    assert ok, reason


# ── H8: ethCall method allowlist (JS layer) ────────────────────────────


def test_runner_js_rejects_cheat_codes():
    """runner.js must reject any RPC method starting with anvil_ /
    hardhat_ / evm_ / debug_ / trace_ / etc. We assert by inspecting the
    source — running Node inside pytest is overkill for a single
    regression."""
    from pathlib import Path
    runner = Path(__file__).resolve().parents[2] / "minotaur_subnet" / "engine" / "runner.js"
    src = runner.read_text()
    assert "REJECTED_RPC_PREFIXES" in src
    assert '"anvil_"' in src
    assert '"hardhat_"' in src
    assert '"evm_"' in src
    assert "ALLOWED_RPC_METHODS" in src
    assert "_validateRpcMethod" in src


def test_runner_js_blocks_metadata_endpoint():
    """169.254.169.254 (cloud metadata) and host.docker.internal must be
    in the blocked-hosts list."""
    from pathlib import Path
    runner = Path(__file__).resolve().parents[2] / "minotaur_subnet" / "engine" / "runner.js"
    src = runner.read_text()
    assert "169.254.169.254" in src
    assert "host.docker.internal" in src
    assert "docker-socket-proxy" in src


# ── H8: leader URL hardening ───────────────────────────────────────────


def test_http_leader_url_rejected():
    """Constructing ValidatorAppCatalogSync with an http:// URL must
    raise unless MINOTAUR_LEADER_ALLOW_HTTP=1."""
    import os
    from minotaur_subnet.validator.app_sync import ValidatorAppCatalogSync

    os.environ.pop("MINOTAUR_LEADER_ALLOW_HTTP", None)
    store = MagicMock()
    with pytest.raises(ValueError, match="https"):
        ValidatorAppCatalogSync(store=store, leader_url="http://leader.example.com")


def test_https_leader_url_accepted():
    from minotaur_subnet.validator.app_sync import ValidatorAppCatalogSync

    store = MagicMock()
    sync = ValidatorAppCatalogSync(store=store, leader_url="https://leader.example.com")
    assert sync.leader_url == "https://leader.example.com"


# ── H9: sandbox overload exception is exported ─────────────────────────


def test_sandbox_overloaded_error_exported():
    from minotaur_subnet.engine import SandboxOverloadedError, JsSandboxError
    # Must be a subclass of JsSandboxError so existing exception handlers
    # don't accidentally swallow it as a generic sandbox error before the
    # 503 mapping kicks in.
    assert issubclass(SandboxOverloadedError, JsSandboxError)
