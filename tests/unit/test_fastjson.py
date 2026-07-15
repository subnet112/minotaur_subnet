"""fastjson: orjson-backed (de)serialization with a stdlib fallback.

Output must stay valid JSON so a store file written by either backend is
readable by the other (rolling-deploy safe), and a serializer edge case
(e.g. an int beyond 64-bit that orjson rejects) must fall back to the stdlib
rather than lose a store write.
"""
from __future__ import annotations

import json

from minotaur_subnet.harness import fastjson


def test_round_trip_basic_types():
    obj = {"a": 1, "b": [1, 2.5, "x", None, True], "c": {"n": "🚀wei"}, "t": 1.6e9}
    assert fastjson.loads(fastjson.dumps(obj)) == obj


def test_cross_compat_both_directions():
    """A file written by fastjson must be readable by the stdlib and vice
    versa (leader on new code, follower/tooling on old, during a rollout)."""
    obj = {"sub_1": {"status": "scored", "epoch": 42, "screening": {"k": ["v"]}}}
    # fastjson (orjson bytes) → stdlib
    assert json.loads(fastjson.dumps(obj).decode()) == obj
    # stdlib (str and bytes) → fastjson
    assert fastjson.loads(json.dumps(obj)) == obj
    assert fastjson.loads(json.dumps(obj).encode()) == obj


def test_edge_int_beyond_64bit_falls_back_not_lost():
    """orjson rejects ints outside the 64-bit range; the stdlib handles them.
    A write must never be lost to that edge — fastjson falls back."""
    obj = {"huge": 2**70, "neg": -(2**66)}
    blob = fastjson.dumps(obj)
    assert fastjson.loads(blob) == obj
    assert json.loads(blob.decode()) == obj


def test_indent_is_valid_and_round_trips():
    obj = {"x": {"y": 1}, "z": [1, 2]}
    blob = fastjson.dumps(obj, indent=True)
    assert b"\n" in blob  # pretty-printed
    assert fastjson.loads(blob) == obj
    assert json.loads(blob.decode()) == obj


def test_returns_bytes():
    assert isinstance(fastjson.dumps({"a": 1}), bytes)
    assert isinstance(fastjson.dumps({"a": 1}, indent=True), bytes)
