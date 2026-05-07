"""Shared helper functions used by multiple service modules."""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

from minotaur_subnet.shared.types import (
    AppIntentConfig,
    TriggerType,
)

import logging

logger = logging.getLogger(__name__)


def _generate_app_id() -> str:
    """Generate a unique app ID."""
    return f"app_{uuid.uuid4().hex[:12]}"


def _generate_wallet_address() -> str:
    """Generate a random dev wallet address (local fallback only)."""
    return "0x" + os.urandom(20).hex()


def _sha256(data: str) -> str:
    """Return the hex SHA-256 hash of a string."""
    return hashlib.sha256(data.encode()).hexdigest()


def _typed_manifest_from_raw(
    raw_manifest: dict[str, Any] | None,
    *,
    app_name: str = "",
):
    """Convert a raw JS manifest to the typed V3 model with safe error capture."""
    if not isinstance(raw_manifest, dict):
        return None, []

    from minotaur_subnet.v3.manifest import manifest_from_legacy_dict

    try:
        return manifest_from_legacy_dict(raw_manifest, app_name=app_name), []
    except Exception as exc:
        return None, [f"Invalid manifest semantics: {exc}"]


def _config_from_manifest(
    supported_chains: list[int],
    typed_manifest,
    *,
    base_config: AppIntentConfig | None = None,
) -> AppIntentConfig:
    """Build AppIntentConfig defaults aligned with a validated typed manifest."""
    trigger_type = (
        TriggerType.AUTO_TRIGGERED
        if any(
            fn.trigger_type == TriggerType.AUTO_TRIGGERED
            for fn in typed_manifest.intent_functions
        )
        else TriggerType.USER_TRIGGERED
    )
    base = base_config or AppIntentConfig()
    return AppIntentConfig(
        supported_chains=supported_chains,
        score_threshold=base.score_threshold,
        on_chain_threshold=base.on_chain_threshold,
        trigger_type=trigger_type,
        max_gas=base.max_gas,
        policy_tier=typed_manifest.default_policy_tier,
        supported_policy_tiers=list(typed_manifest.supported_policy_tiers),
        manifest_version=typed_manifest.manifest_version,
    )


def _validate_manifest_semantics_for_response(
    raw_manifest: dict[str, Any] | None,
    *,
    app_name: str = "",
    config: AppIntentConfig | None = None,
) -> tuple[list[str], list[str], Any | None]:
    """Validate extracted manifest semantics and return response-ready messages."""
    typed_manifest, parse_errors = _typed_manifest_from_raw(raw_manifest, app_name=app_name)
    if parse_errors:
        return parse_errors, [], None
    if typed_manifest is None:
        return [], [], None

    from minotaur_subnet.v3.manifest import validate_manifest_semantics

    manifest_result = validate_manifest_semantics(typed_manifest, config=config)
    return list(manifest_result.errors), list(manifest_result.warnings), typed_manifest
