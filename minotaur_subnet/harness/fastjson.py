"""Fast JSON (de)serialization for the large store files.

The whole ``submissions.json`` (~44MB) is re-serialized on every store write, and
``json.dumps``/``json.loads`` are CPU-bound C calls that HOLD the GIL for their
full duration — so even running the persist on the writer thread (see
``SubmissionStore.aoffload``) does not free the event loop while the encode/decode
runs. ``orjson`` is ~5-10x faster, which shrinks that GIL-held window from
hundreds of ms to tens of ms per write, making the writer-thread offload actually
pay off.

Falls back to the stdlib both when ``orjson`` isn't installed AND, per call, when
orjson's stricter encoder rejects a value the stdlib would accept (e.g. an int
outside the 64-bit range) — so a write is never lost to a serializer edge case.
Output is always valid JSON, so a file written by either path is readable by the
other (rolling-deploy safe: old code reading a new file and vice versa).
"""
from __future__ import annotations

import json
from typing import Any

try:
    import orjson

    _HAVE_ORJSON = True
except ImportError:  # pragma: no cover - orjson is an optional accelerator
    orjson = None  # type: ignore[assignment]
    _HAVE_ORJSON = False


def have_orjson() -> bool:
    return _HAVE_ORJSON


def dumps(obj: Any, *, indent: bool = False) -> bytes:
    """Serialize ``obj`` to UTF-8 JSON bytes (compact by default)."""
    if _HAVE_ORJSON:
        try:
            return orjson.dumps(obj, option=orjson.OPT_INDENT_2 if indent else 0)
        except (TypeError, ValueError):
            # orjson is stricter than the stdlib (e.g. ints beyond 64-bit, or an
            # unexpected type). Fall back so a write can never be lost to a
            # serializer edge case that json.dumps would have handled. A genuinely
            # unserializable object still raises here, exactly as before.
            pass
    return json.dumps(obj, indent=2 if indent else None).encode("utf-8")


def loads(data: bytes | str) -> Any:
    """Parse JSON from bytes or str."""
    if _HAVE_ORJSON:
        return orjson.loads(data)
    # stdlib json.loads accepts str, bytes, and bytearray (3.6+).
    return json.loads(data)
