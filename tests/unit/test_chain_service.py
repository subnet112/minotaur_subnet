"""Unit tests for the public chain-list projection."""

from __future__ import annotations

from types import SimpleNamespace

from minotaur_subnet.api.services.chain_service import build_public_chain_info


def _chain(
    *,
    chain_id: int,
    name: str,
    rpc_url: str = "http://localhost:8545",
    validator_registry_address: str = "",
) -> SimpleNamespace:
    """Build a stand-in for relayer.chain_config.ChainDeployment.

    Only the fields build_public_chain_info reads need to be present.
    """
    return SimpleNamespace(
        chain_id=chain_id,
        name=name,
        rpc_url=rpc_url,
        validator_registry_address=validator_registry_address,
    )


def test_includes_chains_with_validator_registry():
    chains = [
        _chain(chain_id=8453, name="Base", validator_registry_address="0xabc"),
        _chain(chain_id=964, name="Bittensor EVM", validator_registry_address="0xdef"),
    ]
    info = build_public_chain_info(chains)
    assert [c["chain_id"] for c in info] == [8453, 964]
    assert info[0]["registry_address"] == "0xabc"


def test_excludes_internal_only_rpc_without_registry():
    """The simulation-only Anvil fork in prod has ANVIL_RPC_URL set but no
    ValidatorRegistry deployed — it must not leak into the public list."""
    chains = [
        _chain(chain_id=8453, name="Base", validator_registry_address="0xabc"),
        _chain(chain_id=31337, name="Anvil", validator_registry_address=""),
        _chain(chain_id=964, name="Bittensor EVM", validator_registry_address="0xdef"),
    ]
    info = build_public_chain_info(chains)
    ids = [c["chain_id"] for c in info]
    assert 31337 not in ids
    assert ids == [8453, 964]


def test_local_testnet_anvil_with_registry_stays_exposed():
    """In the local-testnet env we DO deploy ValidatorRegistry to chain 31337,
    so it should be treated as a real public chain (not filtered)."""
    chains = [
        _chain(chain_id=31337, name="Anvil", validator_registry_address="0x123"),
    ]
    info = build_public_chain_info(chains)
    assert [c["chain_id"] for c in info] == [31337]


def test_rpc_available_reflects_rpc_url():
    chains = [
        _chain(chain_id=1, name="Ethereum", rpc_url="https://eth.rpc",
               validator_registry_address="0x1"),
        _chain(chain_id=10, name="Optimism", rpc_url="",
               validator_registry_address="0x2"),
    ]
    info = build_public_chain_info(chains)
    info_by_id = {c["chain_id"]: c for c in info}
    assert info_by_id[1]["rpc_available"] is True
    assert info_by_id[10]["rpc_available"] is False


def test_empty_input_yields_empty_output():
    assert build_public_chain_info([]) == []
