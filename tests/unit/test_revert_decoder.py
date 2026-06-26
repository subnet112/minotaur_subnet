"""decode_revert_data — EVM revert payload decoding, incl. the EphemeralProxy
``CallFailed(index, reason)`` wrapper unwrap (so a miner sees the REAL cause of a
failed plan interaction, not just which one failed)."""

from eth_abi import encode
from web3 import Web3

from minotaur_subnet.simulator.revert_decoder import decode_revert_data

_ERROR = bytes.fromhex("08c379a0")
_CALLFAILED = bytes.fromhex("5c0dee5d")


def _error(msg: str) -> bytes:
    return _ERROR + encode(["string"], [msg])


def _callfailed(index: int, reason: bytes) -> bytes:
    return _CALLFAILED + encode(["uint256", "bytes"], [index, reason])


def test_error_string():
    assert decode_revert_data(_error("Too little received")) == 'Error("Too little received")'


def test_panic():
    panic = bytes.fromhex("4e487b71") + encode(["uint256"], [0x11])
    assert decode_revert_data(panic).startswith("Panic(0x11)")


def test_known_custom_error_selector():
    # STF (V3 SwapRouter SafeTransferFrom) from the _CUSTOM_ERRORS table.
    assert "STF" in decode_revert_data(bytes.fromhex("f1ab7b71"))


def test_unknown_custom_error_reports_selector_and_body():
    out = decode_revert_data(bytes.fromhex("deadbeef") + b"\x00" * 32)
    assert "deadbeef" in out and "CustomError" in out


def test_callfailed_unwraps_inner_error():
    """The whole point: CallFailed wrapping an Error shows the inner message."""
    cf = _callfailed(1, _error("Too little received"))
    assert decode_revert_data(cf) == 'CallFailed(index=1, Error("Too little received"))'


def test_callfailed_unwraps_inner_known_custom_error():
    cf = _callfailed(0, bytes.fromhex("f1ab7b71"))  # inner = STF
    out = decode_revert_data(cf)
    assert out.startswith("CallFailed(index=0,") and "STF" in out


def test_callfailed_empty_reason():
    assert decode_revert_data(_callfailed(2, b"")) == "CallFailed(index=2, (no inner reason))"


def test_callfailed_nested_is_depth_bounded():
    """A CallFailed wrapping a CallFailed wrapping ... must terminate, not recurse
    forever."""
    payload = _error("boom")
    for i in range(8):
        payload = _callfailed(i, payload)
    out = decode_revert_data(payload)
    assert out.startswith("CallFailed(") and "nested too deep" in out


def test_real_selector_matches_contract():
    """Guard: the CallFailed selector must match EphemeralProxy's signature."""
    assert _CALLFAILED == Web3.keccak(text="CallFailed(uint256,bytes)")[:4]
