"""Chain-964 call values must cross the wire as STRINGS, not JSON numbers.

The Chopsticks sidecar is JavaScript. A bare JSON number is parsed as a double
and handed to polkadot.js, whose U256 codec refuses anything above
Number.MAX_SAFE_INTEGER (9,007,199,254,740,991 wei ~ 0.009 TAO):

    createType(PrimitiveTypesU256):: Number needs to be an integer
    <= Number.MAX_SAFE_INTEGER

So every value-bearing call carrying a realistic TAO amount failed at the RPC
layer, for every solver, however correct the plan. Measured against the live
bench sidecar 2026-08-25: as an int, 6.993e18 raises the error; as a decimal or
hex string the identical call succeeds.

`set_balance` already stringified, which is exactly why FUNDING always worked
while SPENDING never did — the two sides of the same wire disagreed.
"""
from __future__ import annotations

import json

from minotaur_subnet.simulator.subtensor_simulator import SubtensorSimulator

MAX_SAFE = 2**53 - 1
SEVEN_TAO = 6_993_000_000_000_000_000   # 7 wTAO bridged at 5 bps, in wei


class _Recorder(SubtensorSimulator):
    """Captures the params instead of reaching the sidecar."""

    def __init__(self):
        super().__init__(sidecar_url="http://sidecar:8545")
        self.sent = None

    def _rpc(self, method, params=None, url=None, timeout=None):
        # `timeout` mirrors the real signature: the scoreIntent read passes a
        # long per-call budget (a cold Chopsticks fork costs ~60-90s before it
        # does any work), so the stub must accept it or every call here raises.
        self.sent = (method, params)
        return {}


def _value_of(sim):
    return sim.sent[1][0]["value"]


def test_a_realistic_tao_value_is_not_a_json_number():
    sim = _Recorder()
    sim.eth_call(to="0xdead", data="0x", value=SEVEN_TAO)
    v = _value_of(sim)
    assert isinstance(v, str), "a JSON number here is rejected by the sidecar's U256 codec"
    assert v == str(SEVEN_TAO)


def test_it_survives_the_wire_without_precision_loss():
    """The actual failure was lossy JSON round-tripping, so assert on JSON."""
    sim = _Recorder()
    sim.eth_call(to="0xdead", data="0x", value=SEVEN_TAO)
    wire = json.loads(json.dumps(sim.sent[1]))
    assert int(wire[0]["value"]) == SEVEN_TAO


def test_values_above_max_safe_integer_are_the_whole_point():
    """0.01 TAO already exceeds it — this is not an exotic edge case."""
    assert 10**16 > MAX_SAFE
    sim = _Recorder()
    sim.eth_call(to="0xdead", data="0x", value=10**16)
    assert int(_value_of(sim)) == 10**16


def test_zero_and_none_are_still_zero():
    for given in (0, None):
        sim = _Recorder()
        sim.eth_call(to="0xdead", data="0x", value=given)
        assert _value_of(sim) == "0", given


def test_a_small_value_is_unchanged_in_magnitude():
    """Below MAX_SAFE nothing was broken; do not let the fix move those."""
    sim = _Recorder()
    sim.eth_call(to="0xdead", data="0x", value=500 * 10**9)
    assert int(_value_of(sim)) == 500 * 10**9


def test_set_balance_still_stringifies():
    """The half that always worked, pinned so the two sides cannot drift apart."""
    sim = _Recorder()
    sim.set_balance("0xdead", 100_000 * 1_000_000_000)
    assert sim.sent[0] == "anvil_setBalance"
    assert isinstance(sim.sent[1][1], str)
