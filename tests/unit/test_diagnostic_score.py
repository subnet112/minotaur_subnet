"""Unit test for the diagnostic image-scoring path.

score_image_diagnostic must benchmark an ARBITRARY image through the EXACT
challenger path: the champion reference-quote anchor + the round pin + the same
_benchmark_submission call run_once uses for a real challenger. This proves a
king-clone is scored identically to how the incumbent is scored (symmetry).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.harness.benchmark_worker import BenchmarkWorker


def _worker():
    w = BenchmarkWorker(
        submission_store=MagicMock(),
        app_store=MagicMock(),
        round_store=MagicMock(),
    )
    w._use_docker = False          # skip the simulator-wired guard
    w._simulator = None
    return w


def test_score_image_diagnostic_uses_champion_reference_anchor():
    w = _worker()
    w._round_store.get_current_round.return_value = SimpleNamespace(round_id="round-1")
    intents = [(SimpleNamespace(app_id="dex"), SimpleNamespace(chain_id=8453), None)]
    refq = {"dex": {"quoted_output": "100"}}
    seen: dict[str, object] = {}

    w._apply_epoch_block_pin = MagicMock()
    w._apply_round_anchored_pin = MagicMock()
    w._load_benchmark_intents = MagicMock(return_value=intents)
    w._build_score_fn = AsyncMock(return_value=object())
    w._enrich_intents_with_manifests = MagicMock(side_effect=lambda x: x)
    w._load_historical_scenarios = MagicMock(return_value=[])
    w._build_reference_quotes = AsyncMock(return_value=refq)

    async def _bench(image, ints, score_fn, *, reference_quotes=None):
        seen["image"] = image
        seen["reference_quotes"] = reference_quotes
        return ["R1", "R2"]

    w._benchmark_submission = AsyncMock(side_effect=_bench)
    w._compute_avg_score = MagicMock(return_value=0.70)
    w._results_to_details = MagicMock(return_value={"scorecard": {}})

    out = asyncio.run(w.score_image_diagnostic("king:exact"))

    # benchmarked the GIVEN image against the CHAMPION reference anchor (challenger path)
    assert seen["image"] == "king:exact"
    assert seen["reference_quotes"] is refq
    assert out["score"] == 0.70
    assert out["intent_count"] == 2
    assert out["image"] == "king:exact"
    # applied the deterministic round-anchored pin, exactly like run_once
    w._apply_round_anchored_pin.assert_called_once_with("round-1")
