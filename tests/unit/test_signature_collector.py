"""Unit tests for the signature collector."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from minotaur_subnet.consensus.protocol_config import ProtocolConfig
from minotaur_subnet.relayer.signature_collector import SignatureCollector


def _cfg(quorum_bps: int) -> ProtocolConfig:
    return ProtocolConfig(
        quorum_bps=quorum_bps,
        rpc_url="",
        registry_address="",
    )


@pytest.fixture
def collector():
    return SignatureCollector(
        protocol_config=_cfg(8000),
        validators=["0xval1", "0xval2", "0xval3"],
        timeout=60.0,
    )


class TestQuorum:
    def test_quorum_calculation(self, collector):
        # 80% of 3 = 2.4, ceil = 3
        assert collector.quorum_required == 3

    def test_quorum_two_validators(self):
        c = SignatureCollector(protocol_config=_cfg(5000), validators=["0xa", "0xb"])
        assert c.quorum_required == 1

    def test_quorum_single_validator(self):
        c = SignatureCollector(protocol_config=_cfg(10000), validators=["0xa"])
        assert c.quorum_required == 1


class TestAddSignature:
    def test_collect_until_quorum(self, collector):
        # Need 3 of 3
        r1 = collector.add_signature("hash1", "0xval1", b"sig1", order_id="o1")
        assert r1 is None

        r2 = collector.add_signature("hash1", "0xval2", b"sig2", order_id="o1")
        assert r2 is None

        r3 = collector.add_signature("hash1", "0xval3", b"sig3", order_id="o1")
        assert r3 is not None
        assert r3.order_id == "o1"
        assert len(r3.signatures) == 3

    def test_reject_non_validator(self, collector):
        result = collector.add_signature("hash1", "0xunknown", b"sig")
        assert result is None
        assert collector.pending_count == 0

    def test_reject_duplicate(self, collector):
        collector.add_signature("hash1", "0xval1", b"sig1")
        result = collector.add_signature("hash1", "0xval1", b"sig1_dup")
        assert result is None

    def test_different_plan_hashes(self, collector):
        collector.add_signature("hash_a", "0xval1", b"sig1")
        collector.add_signature("hash_b", "0xval2", b"sig2")
        assert collector.pending_count == 2


class TestPrune:
    def test_prune_expired(self, collector):
        collector.add_signature("hash1", "0xval1", b"sig1")
        assert collector.pending_count == 1

        # Prune with future time
        expired = collector.prune_expired(now=time.time() + 120)
        assert "hash1" in expired
        assert collector.pending_count == 0

    def test_prune_keeps_fresh(self, collector):
        collector.add_signature("hash1", "0xval1", b"sig1")
        expired = collector.prune_expired()
        assert len(expired) == 0
        assert collector.pending_count == 1
