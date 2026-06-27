"""The validator daemon's /health ``orderbook`` field must report the DURABLE
order count (``store.count_orders_by_status``), not the in-memory working set.

Regression: OrderSync upserts the full order corpus into the durable app_store,
but ``self.orderbook`` (the in-memory IntentOrderBook) holds only the active set
(and is empty on followers, which don't run the block loop). Sourcing /health from
``self.orderbook.stats()`` therefore under-reported to ``{}`` — which is what made
the validator-health dashboard read ``OrderBook 0`` on every validator even though
78 orders were synced everywhere. The api ``/blockloop/status`` was already fixed
(fdef554) to source from the store; this pins the same for the daemon /health field
the health workflow actually reads.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.validator.main import AppIntentsValidator

DURABLE = {"filled": 32, "rejected": 46}   # what the store holds (all 78)
IN_MEMORY = {"open": 5}                     # the in-memory working set (must NOT win)


def _stub(*, store):
    """Minimal stand-in with only the attrs _handle_health reads."""
    s = SimpleNamespace()
    s.engine = MagicMock()
    s.engine.list_loaded_intents.return_value = []
    s.orderbook = MagicMock()
    s.orderbook.stats.return_value = dict(IN_MEMORY)
    s.store = store
    s.block_loop = SimpleNamespace(running=True)
    s._weights_emitter = None
    s.weights = SimpleNamespace(owner_hotkey="")
    s._metagraph_sync = None
    s._last_emit_state = None
    s._last_successful_emit_state = None
    s._start_time = time.time()
    return s


def _orderbook_from_health(stub) -> dict:
    resp = asyncio.run(AppIntentsValidator._handle_health(stub, MagicMock()))
    assert resp.status == 200
    return json.loads(resp.body)["orderbook"]


def test_health_orderbook_uses_durable_store_not_in_memory():
    store = MagicMock()
    store.count_orders_by_status.return_value = dict(DURABLE)
    assert _orderbook_from_health(_stub(store=store)) == DURABLE  # store wins, not {"open":5}


def test_health_orderbook_falls_back_to_in_memory_on_store_error():
    # A store hiccup must never 500 the health probe — fall back to the in-memory view.
    store = MagicMock()
    store.count_orders_by_status.side_effect = RuntimeError("db locked")
    assert _orderbook_from_health(_stub(store=store)) == IN_MEMORY


def test_health_orderbook_falls_back_when_no_store():
    assert _orderbook_from_health(_stub(store=None)) == IN_MEMORY
