"""Unit tests for the auto-scaled decision-deadline window.

The leader benchmarks challengers SERIALLY (#387), so close→adopt grows ~linearly with
the slate. A fixed decision window aborts contested rounds on the certification deadline
once the slate is large (the live deadline-abort that lost clean dethrones).
``autoscaled_decision_window`` scales the window with the submission count, floored at
the fixed SOLVER_ROUND_DECISION_EPOCHS. Pure function — no chain/Docker/network.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.round_manager import autoscaled_decision_window


def _w(n, floor=30):
    return autoscaled_decision_window(n, base_epochs=10, per_sub_epochs=4, floor_epochs=floor)


class TestAutoscaledDecisionWindow:
    def test_scales_above_floor_with_slate(self):
        assert _w(6) == 34          # 10 + 6*4
        assert _w(12) == 58         # 10 + 12*4

    def test_floor_applies_for_small_slates(self):
        assert _w(3) == 30          # 10 + 12 = 22 -> floored to 30
        assert _w(0) == 30          # 10 -> floored to 30
        assert _w(5) == 30          # 10 + 20 = 30 == floor

    def test_monotonic_nondecreasing_in_n(self):
        prev = 0
        for n in range(0, 25):
            cur = _w(n)
            assert cur >= prev
            prev = cur

    def test_negative_or_garbage_n_clamped(self):
        assert _w(-5) == 30         # negative slate clamped to 0 -> floor

    def test_reproduces_then_fixes_the_live_regression(self):
        # The live abort: a ~20-epoch benchmark overran the floor=15 window.
        # With the auto-scale, a 6-submission round yields 34 — past the ~20 benchmark.
        assert autoscaled_decision_window(6, base_epochs=10, per_sub_epochs=4, floor_epochs=15) == 34

    def test_tunable_base_and_per_sub(self):
        # Operators can dial both knobs via env.
        assert autoscaled_decision_window(10, base_epochs=5, per_sub_epochs=3, floor_epochs=10) == 35
        assert autoscaled_decision_window(1, base_epochs=5, per_sub_epochs=3, floor_epochs=10) == 10
