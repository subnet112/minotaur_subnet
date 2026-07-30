"""SDK contract version — the marker that tells a validator which generation
of the solver SDK a submission vendored.

WHY THIS EXISTS
───────────────
Solvers **vendor** the SDK: ``/app/minotaur_subnet`` inside a solver image is
the miner's OWN copy, frozen at build time. ``import minotaur_subnet`` in the
container resolves there, never to the validator's installed package. That is
versioning by construction — an existing solver keeps behaving exactly as it
did on the day it was built, whatever we change here.

What we could not do, until this constant existed, is *tell which generation
a given solver is*. Every question in the migration plan ("how many pre-marker
solvers are left in the slate?", "is the champion one of them?", "is it safe
to retire this field yet?") was a judgement call rather than a query. This
turns them into a field on the submission record.

ABSENCE IS THE SIGNAL
─────────────────────
Solvers that vendored the SDK before this module existed report **no**
``sdk_version`` at all. So::

    sdk_version is None  ⇔  pre-marker generation

Nothing needs retroactive stamping, and there is no flag day: the detection
works on the fleet exactly as it stands today.

Both directions are wire-compatible, which is what lets this roll out across a
fleet that promotes unevenly:

  - new solver → old validator: ``orchestrator.metadata()`` rebuilds
    ``SolverMetadata`` field-by-field with ``r.get(...)``, never
    ``SolverMetadata(**r)``, so an unknown ``sdk_version`` key is ignored
    rather than raising ``TypeError``.
  - old solver → new validator: the key is absent, ``.get`` yields ``None``,
    and the solver reads as pre-marker.

NAMING — READ THIS BEFORE CALLING ANYTHING "v2"
───────────────────────────────────────────────
"v1" and "v2" are ALREADY TAKEN in this package, on a different axis:
``IntentProcessor`` is v1 and ``IntentSolver`` is v2 (see ``sdk/__init__``).
That axis is about which ABC a miner subclasses. THIS version is about which
generation of the data/field contract they vendored, and the two move
independently. Refer to generations by their number ("pre-marker", "1.x") and
keep the ``sdk_version`` name — calling this "the v2 SDK" will collide with
the ABC axis and confuse every future reader.

WHAT A BUMP MEANS
─────────────────
Bump the MINOR when the contract grows something new that a solver may rely
on (an added ``MarketSnapshot`` field, a new manifest-derived helper). Bump
the MAJOR when a field or symbol in the published-interface table
(``docs/architecture/sdk-v2-migration.md`` §2) is actually retired, which by
policy happens only once the audit shows no live solver depends on it.

Detection only. This constant gates nothing today: no submission is accepted
or rejected on its value. Arming a floor is a separate, deliberate change —
see ``docs/architecture/sdk-v2-migration.md`` §4.
"""

from __future__ import annotations

# Versioned generations of the solver SDK contract. Solvers that vendored an
# earlier copy have no SDK_VERSION symbol at all — see "ABSENCE IS THE
# SIGNAL" above.
#
#   1.0.0 — first marked generation (the marker itself, #1176).
#   1.1.0 — Phase B deprecation generation: MarketSnapshot
#           prices/pool_states/dex_config warn on access; retirement target
#           2026-09-01, evidence-gated (sdk-v2-migration.md Phase C). A
#           solver reporting >= 1.1.0 re-vendored AFTER the warnings existed
#           — exactly the population the Phase C audit wants to measure.
SDK_VERSION = "1.1.0"

__all__ = ["SDK_VERSION"]
