"""Structured classification of terminal order failures.

The ``error`` field on a rejected order is a free-text string assembled at many
different sites (the block loop, the relayer, the fee/consensus gates). Consumers
that need to reason about *why* an order failed — the frontend computing an honest
service-success rate, dashboards, alerting — had to string-match that prose, which
is brittle and drifts every time a message is reworded.

``classify_rejection`` folds every terminal failure into one small, stable enum so
those consumers can filter deterministically. The taxonomy answers the only
question a success-rate needs: *was this a real service failure, and whose?*

    duplicate  The plan was already submitted for this order/fill-round
               (``plan_hash already submitted``). The trade was ALREADY served —
               this is NOT a service failure and must be excluded from any
               failure count. See relayer/safeguards.py.
    user       The user's own fault: they didn't hold/approve the input token
               (funds fault) or their order signature was invalid. The scoring
               fork fabricates balances, so a balance-less order passes scoring
               then reverts at settlement — blameless to the solver (#229).
    solver     The Solving Engine could not produce an acceptable plan: no plan /
               timeout, policy rejection, on-chain score below threshold, or a
               solver-attributable on-chain revert.
    infra      OUR infrastructure failed the order: on-chain score unavailable
               (dual-scoring fail-closed), consensus not reached, relayer gas /
               signing-key / transport problems, a bad-checksum encode, or a
               per-caller rate limit. These are ours to fix, not the user's.
    expired    The order's deadline passed before it could be served.
    other      Terminal ``rejected`` with an error we don't recognize yet.

``classify_rejection`` returns ``None`` for any non-failure status (filled,
cancelled, or still in flight) — the presence of a class is itself the "this
order terminally failed" signal.

This module is intentionally dependency-free (pure string logic) so it can be
imported from the orderbook, the block loop, and the API layer without cycles.
``blockloop.order_processor`` re-exports the user-fault helpers from here so the
attribution the relayer/settlement path uses and the class the API exposes never
drift apart.
"""

from __future__ import annotations


class RejectionClass:
    """Stable string values for the ``rejection_class`` field.

    Kept as plain strings (not an Enum) so they serialize verbatim into JSON
    order records and stay trivially comparable on the frontend.
    """

    DUPLICATE = "duplicate"
    USER = "user"
    SOLVER = "solver"
    INFRA = "infra"
    EXPIRED = "expired"
    OTHER = "other"

    #: Classes that are NOT a service failure — the demand was served (or the
    #: submission was a redundant duplicate of one that was). A frontend
    #: success-rate should treat these as non-failures.
    NON_FAILURE = frozenset({DUPLICATE})


# ── USER-fault revert markers (authoritative for miner blamelessness, #229) ──
# These also drive the relayer/settlement attribution in
# ``blockloop.order_processor`` (which imports them from here), so the class the
# API exposes matches the accounting the block loop actually applied.

#: Revert signatures of a USER-side signature fault at settlement. The plan
#: passed JS scoring, on-chain sim scoring, and quorum (scoreIntent never
#: verifies the user sig), so the solver is blameless.
USER_SIG_FAULT_MARKERS = (
    "0xf645eedf",              # ECDSAInvalidSignature (confirmed in prod, #229)
    "0xfce698f7",              # ECDSAInvalidSignatureLength
    "0xd78bce0c",              # ECDSAInvalidSignatureS
    "invalid user signature",  # AppIntentBase revert string (post-#229 contract)
    "ecdsainvalidsignature",   # error name (defensive, if surfaced by name)
)

#: Revert signatures of a USER-side FUNDS fault at settlement: the user does not
#: hold / has not approved the input token, so executeIntent's
#: safeTransferFrom(user, proxy, amount) — or the wTAO fee pull — reverts. The
#: scoring fork fabricates the balance, so a balance-less order still passes
#: scoring + quorum and only fails here.
USER_FUNDS_FAULT_MARKERS = (
    "transfer amount exceeds balance",     # OZ v4 ERC20 (input-token transferFrom)
    "transfer amount exceeds allowance",   # OZ v4 ERC20 (no/insufficient approval)
    "erc20: insufficient allowance",       # OZ v4 ERC20
    "erc20insufficientbalance",            # OZ v5 custom error
    "erc20insufficientallowance",          # OZ v5 custom error
)

#: Human-readable prefixes the settlement path prepends once it has ALREADY
#: attributed the fault (order_processor._handle_settlement). Matching these
#: keeps classification correct even when the underlying revert string is
#: wrapped/truncated.
_USER_FUNDS_PREFIXES = ("user cannot fund order",)
_USER_SIG_PREFIXES = ("user signature rejected",)

#: The relayer's in-memory idempotency guard (relayer/safeguards.py). The trade
#: was already submitted for this order — served, not failed.
_DUPLICATE_MARKERS = ("plan_hash already submitted",)

#: OUR-infrastructure failure markers: validator scoring/consensus, relayer gas /
#: key / transport, address-encode bug, and the per-caller throttle.
_INFRA_MARKERS = (
    "on-chain score unavailable",          # dual-scoring fail-closed
    "dual-scoring fail-closed",
    "consensus not reached",
    "relayer balance too low",
    "requires signing_key",
    "relayer transport",
    "cannot connect to host",
    "exceeded per-window limit",           # per-caller rate limit (our throttle)
    "invalid eip-55 checksum",             # encode bug (see PR #876)
    "only accepts checksum addresses",
)

#: SOLVER-attributable failure markers: the engine couldn't produce a plan the
#: gates would accept.
_SOLVER_MARKERS = (
    "solver produced no plan",
    "no plan",
    "policy rejected plan",
    "below threshold",   # JS sentinel gate: "Score N below threshold N"
    "< threshold",       # on-chain score gate: "…BPS < threshold N"
    "fee certification failed",
)


def is_user_signature_fault(error: str | None) -> bool:
    """True if a settlement revert is a USER signature fault (not solver, #229)."""
    if not error:
        return False
    e = error.lower()
    return any(m in e for m in USER_SIG_FAULT_MARKERS) or any(
        p in e for p in _USER_SIG_PREFIXES
    )


def is_user_fund_fault(error: str | None) -> bool:
    """True if a settlement revert is the USER failing to hold/approve the input
    token (or fee) — not a solver fault. Blameless to the miner (#229)."""
    if not error:
        return False
    e = error.lower()
    return any(m in e for m in USER_FUNDS_FAULT_MARKERS) or any(
        p in e for p in _USER_FUNDS_PREFIXES
    )


def is_user_fault(error: str | None) -> bool:
    """True if a settlement revert is attributable to the USER (bad signature OR
    insufficient input-token balance/allowance) rather than the solver (#229)."""
    return is_user_signature_fault(error) or is_user_fund_fault(error)


def classify_rejection(status: str | None, error: str | None) -> str | None:
    """Fold a terminal order state into a :class:`RejectionClass` value.

    Returns ``None`` for any non-failure status — a filled order, a user
    cancellation, or an order still in flight. A returned class is itself the
    signal that the order terminally failed.

    The order of checks mirrors the attribution the block loop / relayer already
    apply at rejection time, so the exposed class matches the accounting that was
    actually done (e.g. a fund-fault under a generic ``Relayer submission
    failed:`` wrapper still classifies as ``user``, not ``solver``).
    """
    s = (status or "").lower()
    if s == "expired":
        return RejectionClass.EXPIRED
    if s != "rejected":
        # filled / cancelled / open / assigned / … — not a terminal failure.
        return None

    e = (error or "").lower()
    if not e:
        return RejectionClass.OTHER

    # 1) Duplicate: the trade was already submitted for this order — served.
    if any(m in e for m in _DUPLICATE_MARKERS):
        return RejectionClass.DUPLICATE
    # 2) User fault (funds or signature) — checked BEFORE the generic relayer
    #    buckets because a user-funds revert can arrive under a "Relayer
    #    submission failed: Transaction reverted: (…exceeds balance…)" wrapper.
    if is_user_fault(error):
        return RejectionClass.USER
    # 3) OUR infrastructure (scoring/consensus/relayer/encode/throttle).
    if any(m in e for m in _INFRA_MARKERS):
        return RejectionClass.INFRA
    # 4) Solver couldn't produce an acceptable plan.
    if any(m in e for m in _SOLVER_MARKERS):
        return RejectionClass.SOLVER
    # 5) Rejected, but an error shape we haven't taught the classifier yet.
    return RejectionClass.OTHER
