"""Chain 964 composes a plan's interactions, like every other chain.

Chopsticks cannot build blocks (pallet_drand's per-block hook needs a BLS12-381
host function its executor lacks), so every ``ck_ethCall`` is an INDEPENDENT
dry-run against the pinned fork. The old per-interaction loop therefore gave no
composition at all: interaction 2 could not see interaction 1's effects. A
destination leg that wrapped native and then moved the wrapper measured
nothing — as far as the transfer was concerned the wrap had never happened —
while the SAME plan on anvil (chains 1/8453) composed fine. Chain 964 was
silently scored by different rules.

``PlanRunner`` runs every interaction inside ONE call so state composes, and
samples watched addresses' native balances either side of the span (native
movement emits no ERC-20 Transfer log, so a bridge crediting native — Tensorplex
on 964 — was invisible to log-based delivery accounting).

The bytecode-level behaviour is asserted against a real EVM (anvil) when one is
available; the encode/decode plumbing is asserted always.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.simulator import subtensor_simulator as st  # noqa: E402

_RUNTIME_HEX_FILE = _REPO_ROOT / "tools" / "chopsticks-sim" / "PlanRunner.deployed.hex"


def test_selector_matches_the_solidity_signature():
    from eth_hash.auto import keccak
    want = keccak(b"runPlan((address,uint256,bytes)[],address[])")[:4].hex()
    assert st._PLAN_RUNNER_SELECTOR == want


def test_embedded_runtime_matches_the_compiled_artifact():
    """The constant is embedded (like GAS_METER_RUNTIME_HEX) so it ships with
    the image; the .hex file is the compiler's output. They must not drift."""
    if not _RUNTIME_HEX_FILE.exists():
        pytest.skip("compiled artifact not present")
    assert st._PLAN_RUNNER_RUNTIME_HEX == _RUNTIME_HEX_FILE.read_text().strip()


def test_native_sentinel_agrees_with_the_token_registry():
    from minotaur_subnet.blockchain.tokens import NATIVE_SENTINEL
    assert st._NATIVE_SENTINEL == NATIVE_SENTINEL


def test_addr_normalises_and_rejects_junk():
    assert st.SubtensorSimulator._addr("0x" + "AB" * 20) == "0x" + "ab" * 20
    assert st.SubtensorSimulator._addr("") == ""
    assert st.SubtensorSimulator._addr(None) == ""
    assert st.SubtensorSimulator._addr("0xdeadbeef") == ""   # too short
    assert st.SubtensorSimulator._addr("not-an-address") == ""


# ── real-EVM behaviour ───────────────────────────────────────────────────────

_COUNTER_SRC = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
contract Counter {
    uint256 public n;
    function bump() external { n += 1; }
    function read() external view returns (uint256) { return n; }
    function payout(address to, uint256 amt) external {
        (bool ok,) = payable(to).call{value: amt}(""); require(ok);
    }
    receive() external payable {}
}
"""


@pytest.fixture(scope="module")
def anvil(tmp_path_factory):
    if not shutil.which("anvil") or not shutil.which("forge"):
        pytest.skip("foundry (anvil/forge) not installed")
    port = 8599
    proc = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            _rpc(url, "eth_blockNumber", [])
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("anvil did not start")
    yield url
    proc.kill()


def _rpc(url, method, params):
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    msg = json.load(urllib.request.urlopen(req, timeout=20))
    if "error" in msg:
        raise RuntimeError(msg["error"])
    return msg["result"]


def _counter_runtime(tmp_path):
    src = tmp_path / "Counter.sol"
    src.write_text(_COUNTER_SRC)
    out = tmp_path / "out"
    subprocess.run(
        ["forge", "build", "--contracts", str(src), "--out", str(out),
         "--cache-path", str(tmp_path / "cache")],
        check=True, capture_output=True,
    )
    art = json.loads((out / "Counter.sol" / "Counter.json").read_text())
    return art["deployedBytecode"]["object"]


_EXEC = "0x000000000000000000000000000000000000c0de"
_CTR = "0x00000000000000000000000000000000000c0117"
_DEST = "0x00000000000000000000000000000000000dead1"


def _setup(url, tmp_path):
    _rpc(url, "anvil_setCode", [_EXEC, st._PLAN_RUNNER_RUNTIME_HEX])
    _rpc(url, "anvil_setCode", [_CTR, _counter_runtime(tmp_path)])
    _rpc(url, "anvil_setBalance", [_EXEC, hex(10**18)])
    _rpc(url, "anvil_setBalance", [_CTR, hex(10**18)])
    _rpc(url, "anvil_setBalance", [_DEST, "0x0"])


def _run(url, calls, watch):
    from eth_abi import encode
    data = "0x" + st._PLAN_RUNNER_SELECTOR + encode(
        ["(address,uint256,bytes)[]", "address[]"], [calls, watch]).hex()
    return _rpc(url, "eth_call", [{"to": _EXEC, "from": _EXEC, "data": data}, "latest"])


def _sel(sig):
    from eth_hash.auto import keccak
    return keccak(sig.encode())[:4]


def test_state_composes_across_interactions(anvil, tmp_path):
    """THE regression: two bump()s then read() must see 2, not 1.

    1 is what the per-interaction loop produced — each call an isolated
    dry-run against the same pre-state.
    """
    from eth_abi import decode
    _setup(anvil, tmp_path)
    calls = [(_CTR, 0, _sel("bump()")), (_CTR, 0, _sel("bump()")), (_CTR, 0, _sel("read()"))]
    out = _run(anvil, calls, [])
    _b, _a, rets = decode(["uint256[]", "uint256[]", "bytes[]"], bytes.fromhex(out[2:]))
    assert int.from_bytes(rets[-1], "big") == 2


def test_native_delivery_is_measured(anvil, tmp_path):
    """A native transfer emits no log; only the balance delta reveals it."""
    from eth_abi import decode, encode
    _setup(anvil, tmp_path)
    amount = 7 * 10**15
    payout = _sel("payout(address,uint256)") + encode(["address", "uint256"], [_DEST, amount])
    out = _run(anvil, [(_CTR, 0, payout)], [_EXEC, _DEST])
    before, after, _r = decode(["uint256[]", "uint256[]", "bytes[]"], bytes.fromhex(out[2:]))
    deltas = dict(zip([_EXEC, _DEST], [a - b for b, a in zip(before, after)]))
    assert deltas[_DEST] == amount


def test_a_failing_interaction_reverts_the_whole_plan(anvil, tmp_path):
    """Swallowing it would report a failed leg as one that moved nothing —
    the exact ambiguity the delivery diagnosis exists to remove."""
    _setup(anvil, tmp_path)
    with pytest.raises(RuntimeError):
        _run(anvil, [(_CTR, 0, _sel("bump()")), (_CTR, 0, _sel("nope()"))], [])
