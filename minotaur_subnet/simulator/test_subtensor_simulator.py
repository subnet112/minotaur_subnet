"""Tests for the Bittensor (chain 964) Chopsticks simulation backend.

The dispatch tests run offline. The SN112 stake integration test needs a running
sidecar (``tools/chopsticks-sim/chopsticks_rpc_server.mjs`` pointed at a Chopsticks
fork of Finney); it self-skips when ``SUBTENSOR_SIDECAR_URL`` is unset/unreachable.
"""

import json
import os
import urllib.request
from pathlib import Path

import pytest
from eth_utils import keccak

from minotaur_subnet.chains import registry
from minotaur_subnet.shared.types import ExecutionPlan, Interaction
from minotaur_subnet.simulator.anvil_simulator import MultiChainSimulator
from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator

pytestmark = pytest.mark.asyncio

SN112_UID0_HOTKEY = "0x56426093d1d8298bbc833d8fec69b94733841ebe0f5cebbb29062d5baf58ab5c"
ROUTER = "0x0000000000000000000000000000000000009999"
_HEX = Path(__file__).resolve().parents[2] / "tools" / "chopsticks-sim" / "StakeMeter.deployed.hex"


def _sel(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


def _word(x: int) -> str:
    return f"{x:064x}"


def _b32(h: str) -> str:
    return h[2:] if h.startswith("0x") else h


# ── dispatch (offline) ────────────────────────────────────────────────────────

async def test_964_registry_uses_chopsticks_backend():
    spec = registry.spec(964)
    assert spec is not None
    assert spec.sim_backend == "substrate_chopsticks"
    # every other wired chain stays on the evm/anvil backend
    for cid in (1, 8453, 31337):
        assert registry.spec(cid).sim_backend == "evm"


async def test_multichain_dispatches_964_to_subtensor_backend(monkeypatch):
    # Gate ON: sidecar env set -> 964 uses the substrate backend
    monkeypatch.setenv("BITTENSOR_CHOPSTICKS_SIM_RPC_URL", "http://sidecar-unreachable:9")
    sim = MultiChainSimulator(
        {964: "http://sidecar-unreachable:9", 8453: "http://anvil-unreachable:9"},
    )
    # 964 -> SubtensorSimulator (constructs even when the sidecar is down)
    assert type(sim.simulators[964]).__name__ == "SubtensorSimulator"
    # 8453 -> AnvilSimulator
    assert type(sim.simulators[8453]).__name__ == "AnvilSimulator"


async def test_964_stays_on_anvil_when_sidecar_env_unset(monkeypatch):
    # Gate OFF (default): no sidecar env -> 964 stays on anvil, inert & unchanged
    monkeypatch.delenv("BITTENSOR_CHOPSTICKS_SIM_RPC_URL", raising=False)
    sim = MultiChainSimulator({964: "http://anvil-btevm:8547"})
    assert type(sim.simulators[964]).__name__ == "AnvilSimulator"


async def test_score_intent_calldata_encoder_roundtrips():
    """The ported scoreIntent encoder produces the right selector + a tuple that
    round-trips through abi_decode (offline; no fork)."""
    from eth_abi import decode as abi_decode
    from eth_hash.auto import keccak

    sim = SubtensorSimulator.__new__(SubtensorSimulator)  # no connect
    sim.chain_id = 964
    order = {
        "order_id": "0x" + "ab" * 32,
        "app": "0x0000000000000000000000000000000000009999",
        "intent_selector": "0xdeadbeef",
        "intent_params": "0x" + "11" * 32,
        "submitted_by": "0x000000000000000000000000000000000000c0de",
        "chain_id": 964, "deadline": 123, "nonce": 7,
        "perpetual": False, "max_executions": 1, "cooldown": 0,
    }
    plan = ExecutionPlan(
        intent_id="x",
        interactions=[Interaction(target="0x0000000000000000000000000000000000000805",
                                  value="0", call_data="0xabcd", chain_id=964)],
        deadline=999, nonce=3,
    )
    cd = sim._build_score_intent_calldata(order["app"], order, plan)
    sig = ("scoreIntent((bytes32,address,bytes4,bytes,address,uint256,uint256,"
           "uint256,bool,uint256,uint256),((address,uint256,bytes)[],uint256,uint256,bytes))")
    assert cd[:10] == "0x" + keccak(sig.encode())[:4].hex()  # correct selector
    raw = bytes.fromhex(cd[10:])
    io, ep = abi_decode(
        ["(bytes32,address,bytes4,bytes,address,uint256,uint256,uint256,bool,uint256,uint256)",
         "((address,uint256,bytes)[],uint256,uint256,bytes)"], raw)
    assert io[0] == bytes.fromhex("ab" * 32)          # order_id
    assert io[2] == bytes.fromhex("deadbeef")          # intent_selector
    assert io[3] == bytes.fromhex("11" * 32)           # intent_params
    assert io[6] == 123 and io[7] == 7                 # deadline, nonce
    assert ep[1] == 999 and ep[2] == 3                 # plan deadline, nonce
    assert ep[0][0][0].lower() == plan.interactions[0].target.lower()  # first call target
    assert ep[0][0][2] == bytes.fromhex("abcd")        # first call data


# ── SN112 stake integration (needs a live sidecar) ────────────────────────────

def _sidecar_url() -> str | None:
    url = os.environ.get("SUBTENSOR_SIDECAR_URL")
    if not url:
        return None
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sim_health", "params": []}).encode()
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            if json.loads(r.read()).get("result", {}).get("ok"):
                return url
    except Exception:
        return None
    return None


async def test_simulate_sn112_stake_delivers_alpha():
    url = _sidecar_url()
    if not url:
        pytest.skip("SUBTENSOR_SIDECAR_URL not set/reachable")

    sim = SubtensorSimulator(sidecar_url=url, chain_id=964)
    # deploy the measuring router + fund its coldkey (the account it stakes under)
    sim.set_code(ROUTER, _HEX.read_text().strip())
    coldkey = sim.mapped_account(ROUTER)
    sim.set_balance(ROUTER, 1000 * 1_000_000_000)  # 1000 TAO in rao

    call = (
        _sel("stakeAndMeasure(bytes32,bytes32,uint256,uint256)")
        + _b32(SN112_UID0_HOTKEY) + _b32(coldkey) + _word(112) + _word(1_000_000_000)
    )
    plan = ExecutionPlan(
        intent_id="t-sn112",
        interactions=[Interaction(target=ROUTER, value="0", call_data=call, chain_id=964)],
        deadline=0,
        nonce=0,
    )
    # contract_address + intent_order -> backend builds scoreIntent calldata itself
    # (ported generic encoder) and reads on_chain_score from the App.
    intent_order = {
        "order_id": "ord_sn112_test", "app": ROUTER,
        "submitted_by": "0x000000000000000000000000000000000000c0de",
        "chain_id": 964, "deadline": 0, "nonce": 0, "intent_params": "0x",
    }
    result = await sim.simulate(plan, contract_address=ROUTER,
                                intent_order=intent_order, meter_gas=True)

    assert result.success, result.error
    # StakeMeter.scoreIntent returns (4242, true) -> backend decoded on_chain_score
    assert result.on_chain_score == 4242
    assert result.gas_used > 0
    assert result.gas_metered == result.gas_used
    # the measuring router returns (before, after, delta) in return_data
    rd = next(c for c in result.state_changes if c["type"] == "return_data")
    h = rd["data"][2:] if rd["data"].startswith("0x") else rd["data"]
    before, after, delta = (int(h[i:i + 64], 16) for i in (0, 64, 128))
    assert before == 0
    assert delta > 0
    assert after == delta
    # ...and the typed delivered_output the scorer JS reads as raw_output
    do = next(c for c in result.state_changes if c["type"] == "delivered_output")
    assert do["token"] == "alpha"
    assert int(do["amount"]) == delta
    # re-pin is idempotent (scoring many candidates at one block re-pins once)
    assert sim.pin_read_fork(964, sim._pinned_block) is True
    print(f"\nSN112 stake via SubtensorSimulator: 1 TAO -> {delta} alpha (gas {result.gas_used})")


async def test_subtensor_stake_raw_scorer_emits_delivered_alpha():
    """The raw-output scorer JS reads delivered_output → metadata.raw_output."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    scorer = Path(__file__).resolve().parents[1] / "harness" / "scoring_shadow" / "subtensor_stake_raw.js"
    state = {
        "simulation": {"state_changes": [
            {"type": "return_data", "data": "0x00"},
            {"type": "delivered_output", "token": "alpha", "amount": "219598620325"},
        ]},
        "typed_context": {"min_output_amount": "1"},
    }
    js = (
        f"const m=require({json.dumps(str(scorer))});"
        f"console.log(JSON.stringify(m.score({json.dumps(state)})));"
    )
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["metadata"]["raw_output"] == "219598620325"
    assert res["valid"] is True
    assert res["score"] == 1
