"""Shared fixtures for the unit suite.

``_reset_minotaur_log_levels`` repairs a cross-test logging-state leak: importing
``bittensor`` (e.g. via ``test_hotkey_signature_verify``, whose module-level
``from bittensor import Keypair`` runs at pytest *collection* time) walks the
existing logger registry and raises every ``minotaur_subnet*`` logger to CRITICAL
(level 50). That silently suppresses the INFO/WARNING records that several
``caplog``-based tests assert (``test_reactive_determinism_parity``,
``test_round_anchor_shadow``), making them fail purely on collection order. Resetting
those levels to NOTSET before each test makes log capture order-independent —
``caplog.at_level(INFO)`` then lowers the inherited level as intended.
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_minotaur_log_levels():
    """Reset every ``minotaur_subnet*`` logger to NOTSET before each test.

    Undoes the CRITICAL level bittensor's import imposes, so caplog tests capture
    minotaur logs regardless of whether a bittensor-importing test was collected.
    """
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if (name == "minotaur_subnet" or name.startswith("minotaur_subnet.")) and isinstance(
            obj, logging.Logger
        ):
            obj.setLevel(logging.NOTSET)
    yield
