"""Early-startup env-var sanity checks for operator-friendly diagnostics.

Operators onboarding a new validator routinely forget to ``cp .env.example
.env`` before running ``docker compose up``. When that happens, Docker
compose's ``${VAR}`` interpolation substitutes an empty string for every
unset variable, and the container starts with blank registry addresses.
The downstream errors ("Consensus enabled but no ValidatorRegistry
address provided", "Champion consensus enabled but no
VALIDATOR_REGISTRY_964") are technically correct but don't point at the
root cause: a missing ``.env`` file.

This module's ``check_required_env_or_exit()`` runs at the very top of
the validator daemon's ``main()`` and the api's ``initialize()``,
catches the missing-env case, prints an actionable diagnosis (with
fix commands), and exits with code 78 (``EX_CONFIG``) so the container
fails fast and the operator sees the message at the top of
``docker compose logs`` instead of buried under restart-loop noise.
"""

from __future__ import annotations

import os
import sys
from typing import Iterable


# Exit code 78 == EX_CONFIG in sysexits.h: "Something was found in the
# configuration file rather than the input that was not in a valid
# format." Right code for "your env is misconfigured."
_EXIT_CODE_BAD_CONFIG = 78


def _is_empty(name: str) -> bool:
    """A var is 'effectively empty' if it's unset OR set to empty string."""
    return not os.environ.get(name, "").strip()


def check_required_env_or_exit(
    required: Iterable[str],
    *,
    process_name: str = "minotaur",
) -> None:
    """Verify each name in ``required`` is non-empty in os.environ.

    If any are missing/empty, print an actionable diagnosis to stderr
    (pointing at the most common cause — missing ``.env`` file) and
    exit the process with ``EX_CONFIG`` (78). If all are set, return
    silently.

    Called at the very top of validator daemon's ``main()`` and the api's
    ``initialize()`` so the message reaches the operator before any
    other setup runs.
    """
    missing = [name for name in required if _is_empty(name)]
    if not missing:
        return

    bar = "=" * 72
    msg = (
        "\n"
        f"{bar}\n"
        f"❌ {process_name}: required environment variables are missing\n"
        f"{bar}\n"
        f"  Empty or unset: {', '.join(missing)}\n"
        "\n"
        "  Most common cause: the .env file next to docker-compose.yml\n"
        "  doesn't exist (or doesn't have these variables). Docker\n"
        "  compose's ${VAR} interpolation silently substitutes an empty\n"
        "  string for unset variables — every downstream startup error\n"
        "  about ValidatorRegistry / quorum / consensus traces back here.\n"
        "\n"
        "  Fix (run from the directory containing docker-compose.yml):\n"
        "\n"
        "    cp .env.example .env\n"
        "    # Edit .env: fill the YOUR_* fields (wallet, hotkey, EVM\n"
        "    # key, axon URL, Alchemy/Infura RPC keys). The registry\n"
        "    # addresses are pre-filled — don't change them.\n"
        "    docker compose down\n"
        "    docker compose --profile autoupdate up -d\n"
        "    docker compose logs -f\n"
        "\n"
        "  If .env is present and these are still empty, your .env may\n"
        "  be stale — re-copy from .env.example, which ships current\n"
        "  defaults for chain addresses.\n"
        f"{bar}\n"
    )
    print(msg, file=sys.stderr, flush=True)
    sys.exit(_EXIT_CODE_BAD_CONFIG)


# Canonical required-set for the validator daemon + api. Both processes
# need both registries to set up consensus signing (order on Base,
# champion on BT EVM).
REQUIRED_REGISTRY_ENV = (
    "VALIDATOR_REGISTRY_8453",
    "VALIDATOR_REGISTRY_964",
)
