"""Champion proposal nonce is floored against the on-chain per-signer high-water.

The leader mints the champion EIP-712 nonce from wall-clock ms. ``ChampionRegistry
.certify()`` enforces ``require(nonces[i] > lastNonce[signer], "Nonce not
increasing")`` per signer, and the relayer swallows that revert to ``None`` (round
aborts ``merge_failed`` with no nonce diagnosis). So a BACKWARD wall-clock movement
on the leader (NTP step-back, VM migration, restart onto a skewed host, or a leader
change to a lagging-clock validator) mints a stale nonce and SILENTLY bricks every
future champion certification for the affected signer(s) until wall-clock catches up.

``_floor_champion_nonce`` reads ``lastNonce[signer]`` for the whole committee and
mints ``max(wall_clock, max_highwater + 1)`` so the nonce is always strictly greater.
It is FAIL-OPEN: any chain-read failure falls back to the wall-clock value (never
blocks proposing). Followers reuse the leader's nonce verbatim and never re-floor.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.api.routes.submissions import champion_consensus as cc  # noqa: E402


def _mgr(highwater_by_signer, *, validators=None, validator_id="0xAA",
         rpc_url="http://bt-evm:9944", quorum_address="0xCHAMP",
         registry_address="0xVALREG", protocol_config=True):
    """Fake ChampionConsensusManager: just the attrs the floor helper reads."""
    pc = None
    if protocol_config:
        pc = SimpleNamespace(
            rpc_url=rpc_url,
            quorum_address=quorum_address,
            registry_address=registry_address,
        )
    return SimpleNamespace(
        protocol_config=pc,
        validators=validators if validators is not None else [validator_id],
        validator_id=validator_id,
    )


def _reader_from(highwater_by_signer):
    """Build a nonce_reader returning a per-signer high-water; records calls."""
    calls = []

    def _reader(rpc_url, registry_address, signer):
        calls.append((rpc_url, registry_address, signer))
        return int(highwater_by_signer.get(signer, 0))

    _reader.calls = calls
    return _reader


# ── The brick-prevention cases ────────────────────────────────────────────────

def test_wallclock_ahead_of_highwater_is_unchanged():
    """Normal forward-running clock: nonce passes through untouched."""
    mgr = _mgr({}, validators=["0xAA"])
    reader = _reader_from({"0xAA": 1_000})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 2_000


def test_wallclock_behind_highwater_is_floored_strictly_greater():
    """Backward clock jump: nonce is lifted to high-water + 1 so certify() can't
    revert 'Nonce not increasing'. This is the silent-brick the fix prevents."""
    mgr = _mgr({}, validators=["0xAA"])
    reader = _reader_from({"0xAA": 5_000})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 5_001


def test_wallclock_equal_to_highwater_is_bumped():
    """Equality reverts on-chain (strictly-greater), so it must be bumped."""
    mgr = _mgr({}, validators=["0xAA"])
    reader = _reader_from({"0xAA": 3_333})
    out = cc._floor_champion_nonce(3_333, mgr, nonce_reader=reader)
    assert out == 3_334


def test_floor_clears_the_max_highwater_across_committee():
    """All co-signers share the leader's single nonce and the contract checks it
    against EACH signer's slot — so the floor must clear the MAX, not the leader's."""
    mgr = _mgr({}, validators=["0xAA", "0xBB", "0xCC"])
    reader = _reader_from({"0xAA": 100, "0xBB": 9_000, "0xCC": 50})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 9_001
    # every committee signer was read
    assert {c[2] for c in reader.calls} == {"0xAA", "0xBB", "0xCC"}


# ── Fail-open behaviour (never block proposing) ───────────────────────────────

def test_read_failure_falls_open_to_wallclock():
    """A chain-read error must NOT block proposing — fall back to wall-clock."""
    mgr = _mgr({}, validators=["0xAA"])

    def _boom(rpc_url, registry_address, signer):
        raise RuntimeError("RPC down")

    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=_boom)
    assert out == 2_000


def test_partial_read_failure_falls_open():
    """If ANY committee signer can't be read we can't guarantee the floor clears
    everyone, so fall open to wall-clock (no worse than today's behaviour)."""
    mgr = _mgr({}, validators=["0xAA", "0xBB"])

    def _reader(rpc_url, registry_address, signer):
        if signer == "0xBB":
            raise RuntimeError("transient")
        return 10_000

    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=_reader)
    assert out == 2_000


def test_no_protocol_config_falls_open():
    mgr = _mgr({}, protocol_config=False)
    reader = _reader_from({"0xAA": 9_999})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 2_000
    assert reader.calls == []  # never reached the chain


def test_missing_rpc_or_registry_falls_open():
    mgr = _mgr({}, rpc_url="", quorum_address="", registry_address="")
    reader = _reader_from({"0xAA": 9_999})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 2_000
    assert reader.calls == []


def test_none_consensus_manager_falls_open():
    reader = _reader_from({})
    out = cc._floor_champion_nonce(2_000, None, nonce_reader=reader)
    assert out == 2_000


# ── Address resolution ────────────────────────────────────────────────────────

def test_reads_from_champion_registry_quorum_address_not_validator_registry():
    """ChampionRegistry (quorum_address) holds lastNonce — NOT the ValidatorRegistry."""
    mgr = _mgr({}, validators=["0xAA"], quorum_address="0xCHAMP",
               registry_address="0xVALREG")
    reader = _reader_from({"0xAA": 1})
    cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert reader.calls[0][1] == "0xCHAMP"


def test_falls_back_to_registry_address_when_no_quorum_address():
    """Single-contract topology: quorum_address unset → use registry_address
    (mirrors _read_quorum_bps' fallback)."""
    mgr = _mgr({}, validators=["0xAA"], quorum_address="", registry_address="0xVALREG")
    reader = _reader_from({"0xAA": 1})
    cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert reader.calls[0][1] == "0xVALREG"


def test_falls_back_to_validator_id_when_validators_empty():
    """No discovered committee (single-validator test setups) → floor the leader's
    own address."""
    mgr = _mgr({}, validators=[], validator_id="0xLEAD")
    reader = _reader_from({"0xLEAD": 7_000})
    out = cc._floor_champion_nonce(2_000, mgr, nonce_reader=reader)
    assert out == 7_001
    assert reader.calls[0][2] == "0xLEAD"


# ── builder-level wiring: floor ONLY on fresh mint, never on nonce_override ──────
#
# The 12 cases above exercise _floor_champion_nonce in isolation; none assert the
# WIRING in _build_champion_proposal_for_round. A regression that moved/added the
# floor call onto the nonce_override (follower / re-broadcast) path would pass all
# of them while silently breaking consensus — a follower that re-floored would sign
# a different nonce than the leader, diverging the EIP-712 digest. These lock that
# invariant directly at the builder.


def _wire_builder(monkeypatch, floor_returns=99_999):
    """Patch the builder's heavy deps + spy on _floor_champion_nonce. Returns the
    list that records floor calls (so a test can assert called / not-called)."""
    candidate = SimpleNamespace(
        submission_id="sub_x", image_digest=None, image_id="sha256:" + "b" * 64,
        hotkey="5Gminer", repo_url="https://github.com/m/s", commit_hash="c" * 40,
    )
    monkeypatch.setattr(cc, "get_store", lambda: SimpleNamespace(get=lambda sid: candidate))
    monkeypatch.setattr(
        cc, "get_champion_consensus_manager",
        lambda: SimpleNamespace(quorum_required=1, committee_hash="committee"),
    )
    floor_calls: list = []
    monkeypatch.setattr(
        cc, "_floor_champion_nonce",
        lambda wallclock_ms, mgr, **kw: floor_calls.append(wallclock_ms) or floor_returns,
    )
    return floor_calls


def _round_state():
    # quorum=1 + image_digest=None → the digest gate is skipped (legacy id OK).
    return SimpleNamespace(
        round_id="round-1", finalist_submission_id="sub_x", quorum_required=1,
        committee_hash="committee", benchmark_pack_hash="pack", effective_epoch=6,
        finalist_image_id=None, shadow_case_log_hash=None, incumbent_image_id=None,
    )


def test_builder_floors_on_fresh_mint(monkeypatch):
    # Leader, no nonce_override → the floor IS applied and its value is used.
    floor_calls = _wire_builder(monkeypatch, floor_returns=99_999)
    proposal = cc._build_champion_proposal_for_round(_round_state())[0]
    assert len(floor_calls) == 1            # floor was consulted exactly once
    assert proposal.nonce == 99_999         # …and its result is the proposal nonce


def test_builder_does_not_refloor_on_override(monkeypatch):
    # Follower / re-broadcast: nonce_override set → the floor MUST NOT be called, and
    # the leader's nonce is reused VERBATIM (re-flooring would diverge the digest).
    floor_calls = _wire_builder(monkeypatch, floor_returns=99_999)
    proposal = cc._build_champion_proposal_for_round(_round_state(), nonce_override=42)[0]
    assert floor_calls == []                # the load-bearing invariant
    assert proposal.nonce == 42             # verbatim passthrough, NOT floored to 99_999


# ── the on-chain reader uses a BOUNDED request timeout ───────────────────────────
#
# read_champion_last_nonce runs inline on the API event loop. The floor is
# fail-open on *exceptions*, but a hung RPC isn't an exception without a timeout —
# it would stall the whole loop. Lock that the provider gets a bounded timeout.


def test_read_champion_last_nonce_uses_bounded_timeout(monkeypatch):
    from unittest.mock import MagicMock
    from minotaur_subnet.consensus import protocol_config as pc

    fake_w3 = MagicMock()
    fake_w3.eth.contract.return_value.functions.lastNonce.return_value.call.return_value = 7

    MockWeb3 = MagicMock()
    MockWeb3.return_value = fake_w3
    MockWeb3.to_checksum_address.side_effect = lambda a: a
    monkeypatch.setattr(pc, "Web3", MockWeb3)
    # The client is now built via build_retrying_web3, which constructs through
    # the `web3.Web3` module attribute — patch that too so the mock is used.
    import web3
    monkeypatch.setattr(web3, "Web3", MockWeb3)

    result = pc.read_champion_last_nonce("http://bt-evm:9944", "0xCHAMP", "0xSigner")

    assert result == 7
    _args, kwargs = MockWeb3.HTTPProvider.call_args
    assert "request_kwargs" in kwargs
    assert kwargs["request_kwargs"].get("timeout") == pc._NONCE_READ_TIMEOUT_SECONDS
