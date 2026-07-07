"""GasMeter end-to-end against a REAL anvil (skipped when anvil is absent).

Drives the REAL ``AnvilSimulator.simulate`` → ``_simulate_via_score_intent``
path — relayer resolution, impersonation self-check, pre-tx on_chain_score
eth_call, GasMeter probe (snapshot-bracketed setCode-at-relayer side tx), and
the direct send — against a local anvil running the vendored ToyScoreApp
(tests/unit/fixtures/ToyScoreApp.sol, solc 0.8.24/cancun/optimizer-200; its
``scoreIntent`` matches the exact tuple signature the simulator encodes,
selector 0x51e02c64).

Asserts the mechanism's contract:
  * ``meter_gas=True`` yields ``gas_metered > 0``, deterministic across runs;
  * score AND receipt-gas parity with ``meter_gas=False`` — the probe is
    state-invisible to the direct send (``gas_used`` semantics untouched);
  * the metered value excludes intrinsic/calldata gas (< receipt gasUsed);
  * a reverting scoreIntent leaves ``gas_metered`` None (no GasMeasured log —
    structural: the meter bubbles reverts before logging).

The RPC-level semantics (EIP-3529 refund invariance, msg.value forwarding,
EIP-3607, log ordering) were spike-proven 36/36 on anvil 1.5.1.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from minotaur_subnet.shared.types import ExecutionPlan

_ANVIL_HOME = os.path.expanduser("~/.foundry/bin/anvil")
_ANVIL_BIN = _ANVIL_HOME if os.path.exists(_ANVIL_HOME) else shutil.which("anvil")

pytestmark = pytest.mark.skipif(
    _ANVIL_BIN is None, reason="anvil not installed (~/.foundry/bin/anvil)",
)

_FIXTURES = Path(__file__).parent / "fixtures"
RELAYER = "0x1111000000000000000000000000000000001111"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def anvil_app():
    """(rpc_url, app_address): a local anvil with ToyScoreApp deployed.

    The app is deployed BEFORE any AnvilSimulator is constructed so the
    simulator's baseline snapshot (its no-upstream reset anchor) includes
    the contract.
    """
    from web3 import Web3
    from eth_abi import encode as abi_encode

    port = _free_port()
    proc = subprocess.Popen(
        [_ANVIL_BIN, "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    rpc_url = f"http://127.0.0.1:{port}"
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        for _ in range(100):
            try:
                w3.eth.block_number
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.skip("anvil did not come up")

        dev = w3.eth.accounts[0]
        creation = (_FIXTURES / "toyscoreapp.creation.hex").read_text().strip()
        deploy_data = creation + abi_encode(["address"], [RELAYER]).hex()
        tx = w3.provider.make_request(
            "eth_sendTransaction",
            [{"from": dev, "data": deploy_data, "gas": hex(3_000_000)}],
        )
        receipt = w3.eth.wait_for_transaction_receipt(tx["result"], timeout=30)
        app = Web3.to_checksum_address(receipt["contractAddress"])
        yield rpc_url, app
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _order(app: str, intent_params: str = "0x") -> dict:
    return {
        "order_id": "bench_0123456789abcdef",
        "app": app,
        "intent_selector": "0x00000000",
        "intent_params": intent_params,
        "submitted_by": "0x" + "33" * 20,
        "chain_id": 31337,
        "deadline": 2**48,
        "nonce": 1,
        "perpetual": False,
        "max_executions": 1,
        "cooldown": 0,
    }


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent_id="toy:swap", interactions=[], deadline=2**48, nonce=1,
    )


def _simulate(sim, app: str, *, meter_gas: bool, intent_params: str = "0x"):
    return asyncio.run(sim.simulate(
        _plan(),
        contract_address=app,
        intent_order=_order(app, intent_params),
        meter_gas=meter_gas,
    ))


def test_meter_measures_positive_gas_with_score_and_receipt_parity(anvil_app):
    from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

    rpc_url, app = anvil_app
    sim = AnvilSimulator(rpc_url=rpc_url)

    off = _simulate(sim, app, meter_gas=False)
    on = _simulate(sim, app, meter_gas=True)
    on2 = _simulate(sim, app, meter_gas=True)

    # Both modes execute the direct send successfully with a real score.
    assert off.success, off.error
    assert on.success, on.error
    assert off.on_chain_score is not None and 7500 <= off.on_chain_score < 8000

    # Default OFF: no metered value, exactly today's path.
    assert off.gas_metered is None

    # Meter ON: positive pre-refund measurement...
    assert on.gas_metered is not None and on.gas_metered > 0
    # ...that excludes tx intrinsic/calldata gas (bracket is inner-call only).
    assert on.gas_metered < on.gas_used

    # PARITY: the probe is invisible to the direct send — identical score and
    # identical receipt gasUsed (gas_used semantics untouched in both modes).
    assert on.on_chain_score == off.on_chain_score
    assert on.gas_used == off.gas_used

    # Deterministic: same fork state + pinned timestamps -> same measurement.
    assert on2.gas_metered == on.gas_metered
    assert on2.on_chain_score == on.on_chain_score


def test_reverting_score_intent_yields_no_metered_gas(anvil_app):
    from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

    rpc_url, app = anvil_app
    sim = AnvilSimulator(rpc_url=rpc_url)

    # intent_params starting 0xde flips ToyScoreApp's deliberate-revert switch.
    res = _simulate(sim, app, meter_gas=True, intent_params="0xde")
    assert res.success is False
    assert res.gas_metered is None  # no GasMeasured on revert — structural
    assert "deliberate revert" in (res.revert_reason or res.error or "")


def test_meter_probe_leaves_no_relayer_code_behind(anvil_app):
    """After a metered simulate(), the relayer address must be code-less again
    (setCode "0x" + the probe's snapshot revert) — the meter must never leak
    into subsequent sims or the live fork state."""
    from web3 import Web3

    from minotaur_subnet.simulator.anvil_simulator import AnvilSimulator

    rpc_url, app = anvil_app
    sim = AnvilSimulator(rpc_url=rpc_url)
    res = _simulate(sim, app, meter_gas=True)
    assert res.success, res.error

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    code = w3.eth.get_code(Web3.to_checksum_address(RELAYER))
    assert code in (b"", b"\x00") or code.hex() in ("", "0x")
