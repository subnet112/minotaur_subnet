"""Unit tests for solver round epoch clock helpers."""

from __future__ import annotations

import unittest

from minotaur_subnet.epoch.clock import EPOCH_SECONDS, SolverRoundEpochClock


class TestSolverRoundEpochClock(unittest.TestCase):
    def test_from_env_ignores_discontinued_epoch_seconds_var(self):
        # SOLVER_ROUND_EPOCH_SECONDS was a debug/test knob and is consensus
        # critical; from_env must never honor it — every validator uses the
        # fixed EPOCH_SECONDS protocol constant.
        clock = SolverRoundEpochClock.from_env(
            env={"SOLVER_ROUND_EPOCH_SECONDS": "120"}
        )
        self.assertEqual(clock.epoch_seconds, EPOCH_SECONDS)
        self.assertEqual(clock.epoch_seconds, 60)

    def test_from_env_still_honors_block_fallback(self):
        clock = SolverRoundEpochClock.from_env(
            env={"SOLVER_ROUND_EPOCH_BLOCKS": "360"}
        )
        self.assertEqual(clock.epoch_blocks, 360)
        self.assertEqual(clock.epoch_seconds, EPOCH_SECONDS)

    def test_time_mode_uses_unix_epoch_buckets(self):
        clock = SolverRoundEpochClock(epoch_seconds=60)
        self.assertEqual(clock.current_epoch(now=0), 0)
        self.assertEqual(clock.current_epoch(now=59.9), 0)
        self.assertEqual(clock.current_epoch(now=60), 1)
        self.assertEqual(clock.current_epoch(now=125), 2)

    def test_block_mode_uses_block_buckets_when_available(self):
        clock = SolverRoundEpochClock(epoch_seconds=60, epoch_blocks=20)
        self.assertTrue(clock.uses_block_mode(block_number=39))
        self.assertEqual(clock.current_epoch(block_number=39, now=9999), 1)
        self.assertEqual(clock.current_epoch(block_number=40, now=0), 2)

    def test_block_mode_falls_back_to_time_without_block_number(self):
        clock = SolverRoundEpochClock(epoch_seconds=30, epoch_blocks=20)
        self.assertFalse(clock.uses_block_mode(block_number=None))
        self.assertEqual(clock.current_epoch(block_number=None, now=61), 2)

    def test_native_epoch_takes_precedence_over_other_modes(self):
        clock = SolverRoundEpochClock(epoch_seconds=30, epoch_blocks=20)
        self.assertEqual(
            clock.current_epoch(
                block_number=999,
                native_epoch=7,
                native_epoch_length_blocks=361,
                now=9999,
            ),
            7,
        )

    def test_health_snapshot_reports_native_tempo_mode(self):
        clock = SolverRoundEpochClock(epoch_seconds=60, epoch_blocks=20)
        snapshot = clock.health_snapshot(
            block_number=725,
            native_epoch=2,
            native_epoch_length_blocks=361,
            native_blocks_since_last_step=5,
        )
        self.assertEqual(snapshot["mode"], "native_tempo")
        self.assertEqual(snapshot["resolved_epoch_blocks"], 361)
        self.assertEqual(snapshot["native_epoch"], 2)
        self.assertEqual(snapshot["native_blocks_since_last_step"], 5)


if __name__ == "__main__":
    unittest.main()
