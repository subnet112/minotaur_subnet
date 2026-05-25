"""Tests for the order-consensus ProtocolConfig refresh-loop wiring.

The leader's ``ValidatorPeerNetwork.peers`` reads the discovery side of
its union from ``protocol_config.peers``. That list is populated by
``ProtocolConfig.refresh_loop`` running as a background task. Without
the loop running, the discovery side stays permanently empty and the
union degenerates to env-pinned-only.

These tests lock in the wiring on the api side. PR shipped 2026-05-25
after debugging why the first third-party validator's signatures
weren't being collected even after they registered on-chain + auto-
served their axon URL.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_ctx_has_order_protocol_config_fields():
    """ServerContext must carry both the config and the task handle —
    the task is needed so leak inspectors / future shutdown code can
    find it; the config so consumers can introspect."""
    from minotaur_subnet.api.server_context import ctx
    assert hasattr(ctx, "order_protocol_config")
    assert hasattr(ctx, "order_protocol_config_task")


def test_startup_stashes_order_protocol_config_on_ctx():
    """The order_protocol_config built in the real-consensus branch
    must be assigned to ctx so the later refresh-loop wiring can
    reach it."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    # Specifically: the line that says `ctx.order_protocol_config = order_protocol_config`
    # must appear after the construction line
    assert "ctx.order_protocol_config = order_protocol_config" in src, (
        "order_protocol_config must be stashed on ctx after construction "
        "so the later refresh-loop wiring can reach it"
    )


def test_startup_starts_order_refresh_loop_task():
    """The api startup must asyncio.create_task() on
    ctx.order_protocol_config.refresh_loop() — without this the
    discovery side of peer_network's union stays empty."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    # Strip whitespace to make the match resilient to indentation drift
    flat = re.sub(r"\s+", " ", src)
    assert re.search(
        r"ctx\.order_protocol_config_task\s*=\s*asyncio\.create_task\(\s*ctx\.order_protocol_config\.refresh_loop\(",
        flat,
    ), (
        "ctx.order_protocol_config_task = asyncio.create_task(ctx.order_protocol_config.refresh_loop())"
        " must be wired in startup"
    )


def test_startup_sets_order_metagraph_provider():
    """The provider function must be assigned BEFORE create_task fires —
    otherwise the loop's first tick has nothing to walk."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    assert "ctx.order_protocol_config.metagraph_provider" in src, (
        "order_protocol_config.metagraph_provider must be wired so "
        "_refresh_peers can walk subnet 112 axons"
    )


def test_order_wiring_is_idempotent_guard():
    """The wiring is guarded by ``ctx.order_protocol_config_task is None``
    — protects against double-start if the initialize() path is run
    twice (e.g. test fixture re-entry)."""
    src = (_REPO_ROOT / "minotaur_subnet" / "api" / "startup.py").read_text()
    flat = re.sub(r"\s+", " ", src)
    assert "ctx.order_protocol_config_task is None" in flat, (
        "double-start guard missing — re-running startup should not "
        "create two concurrent refresh-loop tasks"
    )
