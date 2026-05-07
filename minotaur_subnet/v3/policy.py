"""Policy models for Architecture V3."""

from __future__ import annotations

from dataclasses import dataclass, field

from minotaur_subnet.shared.types import PolicyTier


@dataclass
class WalletPolicy:
    """Wallet-level policy controls."""

    policy_id: str
    tier: PolicyTier = PolicyTier.HYBRID
    max_notional_usd: float | None = None
    allowed_chains: list[int] = field(default_factory=list)
    allowed_apps: list[str] = field(default_factory=list)
    allowed_tokens: list[str] = field(default_factory=list)
    allowed_protocols: list[str] = field(default_factory=list)
    max_slippage_bps: int | None = None
    cooldown_seconds: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class AppPolicy:
    """App-level policy declaration."""

    app_id: str
    tier: PolicyTier = PolicyTier.HYBRID
    supported_tiers: list[PolicyTier] = field(
        default_factory=lambda: [PolicyTier.STRICT, PolicyTier.HYBRID, PolicyTier.EXPERT]
    )
    allowed_protocols: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class EffectivePolicy:
    """Computed effective policy after combining global/app/wallet/order inputs."""

    tier: PolicyTier
    app_id: str = ""
    wallet_policy_id: str = ""
    allowed_chains: list[int] = field(default_factory=list)
    allowed_apps: list[str] = field(default_factory=list)
    allowed_tokens: list[str] = field(default_factory=list)
    allowed_protocols: list[str] = field(default_factory=list)
    max_notional_usd: float | None = None
    max_slippage_bps: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)
