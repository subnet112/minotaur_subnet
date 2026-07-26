"""Bridge adapter abstract base class and data types.

Defines the interface that all bridge protocol adapters must implement.
Each adapter handles quoting, building bridge calldata, and monitoring
transfer status for a specific bridge protocol (Tensorplex, CCTP, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minotaur_subnet.shared.types import Interaction


class BridgeStatusEnum(str, Enum):
    """Status of an in-flight bridge transfer."""
    PENDING = "pending"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class BridgeQuote:
    """Quote from a bridge protocol for a cross-chain transfer."""
    protocol: str
    src_chain_id: int
    dst_chain_id: int
    token_in: str           # address on source chain
    token_out: str          # address on dest chain (may differ)
    amount_in: int          # wei
    estimated_output: int   # wei on dest chain (after fees)
    fee: int                # bridge fee in source token units
    estimated_duration_s: int  # expected bridge completion time
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BridgeStatus:
    """Status of an in-flight bridge transfer."""
    status: BridgeStatusEnum
    src_tx_hash: str
    dst_tx_hash: str | None = None
    amount_received: int | None = None
    error: str | None = None
    # Adapter-specific payload for the tracker (additive). CCTP uses it to
    # surface the attested message the platform must self-relay on the
    # destination ({ready_to_mint, message, attestation}).
    metadata: dict[str, Any] = field(default_factory=dict)


class BridgeAdapter(ABC):
    """Abstract base for bridge protocol adapters.

    Each adapter handles one bridge protocol (e.g., Tensorplex, CCTP).
    The adapter is responsible for:
    - Quoting bridge fees and estimated output
    - Building the on-chain interaction(s) to initiate a bridge transfer
    - Monitoring bridge transfer status until completion
    """

    PROTOCOL: str = ""

    #: What happens to the user's funds when a transfer does NOT deliver.
    #:
    #: True  — the ORIGIN chain refunds the depositor (Across returns an
    #:         unfilled deposit at ``fillDeadline``). The funds come home by
    #:         themselves, so the only decision left for the user is whether
    #:         to re-quote.
    #: False — the source funds are irreversibly committed and delivery is a
    #:         question of *when*, not *whether*: burn-and-mint (CCTP — the
    #:         burn cannot be undone and the attestation stays valid forever)
    #:         and lock-and-mint (Hyperlane) rails. There is nothing to
    #:         refund and nothing to re-quote; a stalled transfer is an ops
    #:         escalation, not a user choice.
    #:
    #: Defaults False so we never promise a refund we can't verify — the
    #: recovery flow's wording and options branch on this.
    REFUNDS_ON_ORIGIN: bool = False

    @abstractmethod
    async def quote(
        self,
        token_in: str,
        amount: int,
        src_chain_id: int,
        dst_chain_id: int,
    ) -> BridgeQuote:
        """Get a quote for bridging tokens.

        Args:
            token_in: Token address on the source chain.
            amount: Amount to bridge in wei.
            src_chain_id: Source chain ID.
            dst_chain_id: Destination chain ID.

        Returns:
            BridgeQuote with estimated output, fees, and duration.
        """

    @abstractmethod
    def build_bridge_interactions(
        self,
        quote: BridgeQuote,
        recipient: str,
        refund_recipient: str | None = None,
    ) -> list[Interaction]:
        """Build the on-chain interactions to initiate a bridge transfer.

        The two addresses are deliberately SEPARATE — they land in different
        chains' states and want different owners:

        Args:
            quote: A quote from this adapter's ``quote()`` method.
            recipient: Who receives the funds on the DESTINATION chain. For a
                multi-leg intent this is the destination App contract, whose
                ``escrowDeposit`` gate + user-callable ``escrowRefund``
                timelock are what make the hop safe (see
                docs/architecture/cross-chain-intents.md §1, state 3/4).
                Pinned into the deposit at source-commitment time, so no
                downstream party can redirect it.
            refund_recipient: Who receives an ORIGIN-chain refund if the
                bridge never delivers (Across: the depositor, refunded at
                ``fillDeadline``). This must stay the USER — a refund into
                the app would need a second recovery path to get home.
                Defaults to ``recipient`` for rails with no refund concept.

        Returns:
            List of Interactions (e.g., approve + bridge deposit).
        """

    @abstractmethod
    async def check_status(
        self,
        src_tx_hash: str,
        src_chain_id: int,
        dst_chain_id: int = 0,
    ) -> BridgeStatus:
        """Check the status of an in-flight bridge transfer.

        Args:
            src_tx_hash: Transaction hash of the bridge initiation on source chain.
            src_chain_id: Source chain ID.
            dst_chain_id: Destination chain ID (enables on-chain verification).

        Returns:
            BridgeStatus with current state and optional destination tx hash.
        """

    def build_bridge_substrate_actions(
        self,
        quote: BridgeQuote,
        owner_ss58: str,
    ) -> list:
        """Build substrate actions for bridge deposit (Bittensor → EVM).

        Override for bridges where the source chain is Bittensor substrate
        (not EVM). Returns a list of SubstrateAction dicts.

        Default implementation returns empty list (EVM-only bridges).
        """
        return []

    def mock_config(self, quote: "BridgeQuote") -> dict[str, Any]:
        """Return simulation mock configuration for this bridge.

        The simulator uses this to replace bridge calls with mock transfers
        during Anvil fork simulation. Each adapter knows its own selectors
        and how to mock them.

        Returns dict with:
            selectors: list[str] — 4-byte hex selectors to replace
            mock_type: "erc20_transfer" | "noop"
            mock_token: str — token address for mock transfer
            mock_amount: int — amount for mock transfer
        """
        return {
            "selectors": [],
            "mock_type": "noop",
            "mock_token": "",
            "mock_amount": 0,
        }

    @abstractmethod
    def supported_routes(self) -> list[tuple[int, int]]:
        """Return list of supported (src_chain_id, dst_chain_id) pairs."""
