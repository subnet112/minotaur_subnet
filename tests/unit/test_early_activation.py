"""Unit tests for certify-anchored early activation.

The close-time schedule anchors activation at ``close + ACTIVATION_DELAY`` — sized for
the worst-case decision — so a certification landing early idles out the remainder
(~15-30 min per adopt at slate 6). ``early_activation_effective_epoch`` re-anchors the
effective epoch to the actual certification plus a fleet-propagation margin, clamped to
never land later than the close-anchored schedule. Pure function — no chain/Docker.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.api.routes.submissions.round_manager import (
    early_activation_effective_epoch,
)


class TestEarlyActivationEffectiveEpoch:
    def test_disabled_margin_keeps_close_anchored_schedule(self):
        # margin <= 0 is the opt-out default: schedule unchanged, exactly.
        assert early_activation_effective_epoch(1116, 1050, 0) == 1116
        assert early_activation_effective_epoch(1116, 1050, -5) == 1116

    def test_early_certification_activates_at_certify_plus_margin(self):
        # close=1000, close-anchored effective=1116; certification lands at
        # epoch 1060 -> activate at 1070, not 1116 (46 epochs saved).
        assert early_activation_effective_epoch(1116, 1060, 10) == 1070

    def test_never_later_than_close_anchored_schedule(self):
        # A certification landing near (or past) the schedule must not push
        # activation OUT — the clamp keeps the legacy epoch as the ceiling.
        assert early_activation_effective_epoch(1116, 1110, 10) == 1116
        assert early_activation_effective_epoch(1116, 1200, 10) == 1116

    def test_margin_is_a_real_floor_on_propagation_time(self):
        # The certificate needs margin epochs to reach the fleet: activation is
        # never scheduled sooner than certify + margin.
        eff = early_activation_effective_epoch(1116, 1060, 10)
        assert eff - 1060 == 10

    def test_monotonic_in_certify_time_until_clamp(self):
        prev = 0
        for certify_epoch in range(1000, 1130):
            cur = early_activation_effective_epoch(1116, certify_epoch, 10)
            assert cur >= prev
            assert cur <= 1116
            prev = cur
