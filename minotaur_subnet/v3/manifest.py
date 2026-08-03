"""Intent manifest models for Architecture V3."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak

from minotaur_subnet.shared.types import AppIntentDefinition, PolicyTier, TriggerType

logger = logging.getLogger(__name__)


@dataclass
class IntentFieldSpec:
    """Schema for a single intent parameter."""

    name: str
    value_type: str
    required: bool = True
    description: str = ""
    default: Any = None
    source: str = "user"
    # Whether this param is part of the on-chain function SIGNATURE (and thus
    # the intent selector). Computational/quoted params that are appended to
    # intentParams but are NOT in the contract's `<intent>(...)` signature
    # (e.g. platform fee, a quoted-output fee reference, an unwrap flag) set
    # this False so the selector still matches the contract. Default True.
    in_signature: bool = True


@dataclass
class IntentFunctionSpec:
    """Manifest entry for one intent function."""

    name: str
    selector: str = ""
    trigger_type: TriggerType = TriggerType.USER_TRIGGERED
    description: str = ""
    params: list[IntentFieldSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_param(self, name: str) -> IntentFieldSpec | None:
        """Return the named field spec if present."""
        for param in self.params:
            if param.name == name:
                return param
        return None


@dataclass
class IntentManifest:
    """Authoritative app schema for runtime, API, and solver integration."""

    app_name: str
    manifest_version: str = "v1"
    intent_functions: list[IntentFunctionSpec] = field(default_factory=list)
    default_policy_tier: PolicyTier = PolicyTier.HYBRID
    supported_policy_tiers: list[PolicyTier] = field(
        default_factory=lambda: [PolicyTier.STRICT, PolicyTier.HYBRID, PolicyTier.EXPERT]
    )
    simulation_hints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_intent(self, name: str) -> IntentFunctionSpec | None:
        """Return a function spec by intent name."""
        for intent in self.intent_functions:
            if intent.name == name:
                return intent
        return None

    def requires_field(self, intent_name: str, field_name: str) -> bool:
        """Return True when the named field is required for the given intent."""
        intent = self.get_intent(intent_name)
        if intent is None:
            return False
        field = intent.get_param(field_name)
        return bool(field and field.required)


@dataclass
class ManifestValidationResult:
    """Validation result for manifest-level trigger and policy semantics."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def manifest_from_definition(intent: AppIntentDefinition) -> IntentManifest | None:
    """Return a typed manifest for an app definition when one exists."""
    raw_manifest = getattr(intent, "manifest", None)
    if isinstance(raw_manifest, IntentManifest):
        return raw_manifest
    if isinstance(raw_manifest, dict):
        return manifest_from_legacy_dict(
            raw_manifest,
            app_name=intent.name or intent.app_id,
        )
    return None


def manifest_from_legacy_dict(
    raw: dict[str, Any],
    *,
    app_name: str = "",
) -> IntentManifest:
    """Convert the current JS manifest dict shape into a typed IntentManifest."""
    functions: list[IntentFunctionSpec] = []
    for fn_def in raw.get("intent_functions", []) or []:
        raw_params = fn_def.get("params", {}) or {}
        params: list[IntentFieldSpec] = []
        if isinstance(raw_params, dict):
            for field_name, field_def in raw_params.items():
                if isinstance(field_def, dict):
                    params.append(
                        IntentFieldSpec(
                            name=field_name,
                            value_type=field_def.get("type", "any"),
                            required=field_def.get("required", True),
                            description=field_def.get("description", ""),
                            default=field_def.get("default"),
                            source=field_def.get("source", "user"),
                            in_signature=field_def.get("in_signature", True),
                        )
                    )
                else:
                    params.append(
                        IntentFieldSpec(name=field_name, value_type=str(field_def))
                    )
        functions.append(
            IntentFunctionSpec(
                name=fn_def.get("name", ""),
                selector=fn_def.get("selector", ""),
                trigger_type=TriggerType(fn_def.get("trigger_type", TriggerType.USER_TRIGGERED.value)),
                description=fn_def.get("description", ""),
                params=params,
                metadata=fn_def.get("metadata", {}) or {},
            )
        )

    tier_value = raw.get("default_policy_tier", PolicyTier.HYBRID.value)
    supported_tiers = [
        PolicyTier(v) for v in raw.get(
            "supported_policy_tiers",
            [PolicyTier.STRICT.value, PolicyTier.HYBRID.value, PolicyTier.EXPERT.value],
        )
    ]

    return IntentManifest(
        app_name=app_name or raw.get("name", "") or raw.get("app_name", ""),
        manifest_version=raw.get("manifest_version", "v1"),
        intent_functions=functions,
        default_policy_tier=PolicyTier(tier_value),
        supported_policy_tiers=supported_tiers,
        simulation_hints=raw.get("simulation_hints", {}) or {},
        metadata=raw.get("metadata", {}) or {},
    )


def validate_manifest_semantics(
    manifest: IntentManifest,
    *,
    config: Any | None = None,
) -> ManifestValidationResult:
    """Validate policy-tier and trigger semantics for a typed manifest.

    This intentionally focuses on V3 semantic alignment, not exhaustive schema
    validation. It can be used by API/runtime layers without making manifest
    parsing itself stricter for compatibility.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest.intent_functions:
        errors.append("Manifest must declare at least one intent function.")

    seen_names: set[str] = set()
    auto_trigger_present = False
    for fn in manifest.intent_functions:
        if not fn.name:
            errors.append("Manifest intent functions must have non-empty names.")
            continue
        if fn.name in seen_names:
            errors.append(f"Manifest intent function names must be unique: {fn.name!r}.")
        seen_names.add(fn.name)
        if fn.trigger_type == TriggerType.AUTO_TRIGGERED:
            auto_trigger_present = True

    supported = list(manifest.supported_policy_tiers or [])
    if not supported:
        errors.append("Manifest supported_policy_tiers must not be empty.")
    elif len(set(supported)) != len(supported):
        errors.append("Manifest supported_policy_tiers must not contain duplicates.")

    if supported and manifest.default_policy_tier not in supported:
        errors.append(
            "Manifest default_policy_tier must be included in supported_policy_tiers."
        )

    if config is not None:
        expected_trigger = (
            TriggerType.AUTO_TRIGGERED
            if auto_trigger_present
            else TriggerType.USER_TRIGGERED
        )
        if getattr(config, "trigger_type", expected_trigger) != expected_trigger:
            errors.append(
                "App config.trigger_type must be AUTO_TRIGGERED when any manifest "
                "intent function is auto-triggered; otherwise it must remain "
                "USER_TRIGGERED."
            )

        config_policy_tier = getattr(config, "policy_tier", manifest.default_policy_tier)
        if config_policy_tier != manifest.default_policy_tier:
            errors.append(
                "App config.policy_tier must match manifest.default_policy_tier."
            )

        config_supported = list(
            getattr(config, "supported_policy_tiers", manifest.supported_policy_tiers) or []
        )
        if set(config_supported) != set(supported):
            errors.append(
                "App config.supported_policy_tiers must match manifest.supported_policy_tiers."
            )
        elif config_supported != supported:
            warnings.append(
                "App config.supported_policy_tiers uses a different ordering than "
                "manifest.supported_policy_tiers."
            )

    return ManifestValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
    )


def canonical_intent_signature(
    manifest: IntentManifest,
    intent_name: str,
    *,
    skip_fields_with_prefixes: tuple[str, ...] = ("permit_",),
) -> str | None:
    """Build the canonical Solidity signature for an intent from manifest schema."""
    fn = manifest.get_intent(intent_name)
    if fn is None:
        return None

    param_types = [
        field.value_type
        for field in fn.params
        if field.in_signature and not field.name.startswith(skip_fields_with_prefixes)
    ]
    return f"{intent_name}({','.join(param_types)})"


def compute_selector_from_manifest(
    manifest: IntentManifest,
    intent_name: str,
    *,
    skip_fields_with_prefixes: tuple[str, ...] = ("permit_",),
) -> str | None:
    """Compute the 4-byte selector for an intent manifest entry."""
    signature = canonical_intent_signature(
        manifest,
        intent_name,
        skip_fields_with_prefixes=skip_fields_with_prefixes,
    )
    if signature is None:
        return None
    return keccak(signature.encode())[:4].hex()


def selector_from_legacy_manifest(
    manifest: dict[str, Any] | None, intent_function: str,
) -> str:
    """4-byte intent selector for ``intent_function``, from a raw manifest dict.

    The ONE implementation shared by every path that needs a selector without a
    parsed :class:`IntentManifest` in hand — the benchmark intent-order builder
    and the validator scoring engine. Both previously carried their own literal
    ``{swap, execute, buy} -> canonical signature`` map, i.e. one app's ABI in a
    path every app goes through. #1152 removed that map from
    ``blockloop/order_processor`` and left those two copies behind, so the SAME
    intent_function resolved to DIFFERENT selectors depending on which path
    reached it. Sharing one function is what stops that recurring.

    Verified against the live DexAggregator manifest: ``swap`` -> ``d5bcb9b5``,
    identical to the removed map, so this is a drop-in for the app the map was
    written for and correct for an app core has never heard of.

    Falls back to the no-arg ``fn()`` convention when the app declares no such
    intent — what the map did for any unlisted name — and LOGS it, because a
    selector the contract does not implement reverts in scoreIntent and scores
    0, which reads as a bad solver rather than an undeclared intent.
    """
    fn = intent_function or ""
    try:
        if isinstance(manifest, dict) and "intent_functions" in manifest:
            selector = compute_selector_from_manifest(
                manifest_from_legacy_dict(manifest), fn,
            )
            if selector:
                return str(selector).replace("0x", "")
    except Exception as exc:  # noqa: BLE001 — a bad manifest must not stop scoring
        logger.warning("Manifest selector lookup failed for %r: %s", fn, exc)

    logger.warning(
        "No manifest signature for intent %r — falling back to the no-arg "
        "selector convention; scoreIntent reverts if the contract does not "
        "implement %s()", fn, fn,
    )
    return keccak(f"{fn}()".encode())[:4].hex()


def normalize_swap_intent_params(
    params: dict[str, Any],
    *,
    manifest: IntentManifest | None = None,
    intent_name: str = "swap",
    receiver_default: str = "",
    slippage_bps: int | None = None,
) -> dict[str, Any]:
    """Normalize swap params to one runtime shape using manifest hints when present.

    Supports aliases from DCA-style params (tokenIn/tokenOut/amountPerBuy) and
    yield-style params (asset/amount) so the baseline solver can handle multiple
    app types without separate strategies.
    """
    input_token = (
        params.get("input_token", "")
        or params.get("tokenIn", "")
        or params.get("token_in", "")
    )
    output_token = (
        params.get("output_token", "")
        or params.get("tokenOut", "")
        or params.get("token_out", "")
    )

    input_amount_raw = (
        params.get("input_amount", 0)
        or params.get("amountPerBuy", 0)
        or params.get("amount_per_buy", 0)
        or params.get("amount", 0)
    )
    input_amount = int(input_amount_raw or 0)

    min_output_raw = (
        params.get("min_output_amount")
        or params.get("output_amount")
        or params.get("minAmountOut")
        or params.get("min_amount_out")
    )
    if min_output_raw not in (None, ""):
        min_output_amount = int(min_output_raw)
    elif slippage_bps is not None and input_amount > 0:
        min_output_amount = input_amount * (10000 - slippage_bps) // 10000
    else:
        min_output_amount = 0

    receiver_field = canonical_swap_receiver_field(manifest, intent_name=intent_name)
    receiver = _resolve_swap_receiver(
        params,
        canonical_field=receiver_field,
        receiver_default=receiver_default,
    )

    fee_tier = params.get(
        "fee_tier",
        _intent_field_default(manifest, intent_name, "fee_tier", 3000),
    )
    permit_deadline = params.get(
        "permit_deadline",
        _intent_field_default(manifest, intent_name, "permit_deadline", 0),
    )
    permit_v = params.get(
        "permit_v",
        _intent_field_default(manifest, intent_name, "permit_v", 0),
    )
    permit_r = params.get(
        "permit_r",
        _intent_field_default(manifest, intent_name, "permit_r", "0x" + "00" * 32),
    )
    permit_s = params.get(
        "permit_s",
        _intent_field_default(manifest, intent_name, "permit_s", "0x" + "00" * 32),
    )

    return {
        "input_token": input_token,
        "output_token": output_token,
        "input_amount": input_amount,
        "min_output_amount": min_output_amount,
        "receiver": receiver,
        "receiver_field": receiver_field,
        "fee_tier": int(fee_tier or 3000),
        "permit_deadline": int(permit_deadline or 0),
        "permit_v": int(permit_v or 0),
        "permit_r": permit_r,
        "permit_s": permit_s,
    }


def normalize_twap_intent_params(
    params: dict[str, Any],
    *,
    manifest: IntentManifest | None = None,
    intent_name: str = "twap",
    receiver_default: str = "",
    slippage_bps: int | None = None,
) -> dict[str, Any]:
    """Normalize TWAP params to one runtime shape using manifest hints when present."""
    total_amount = int(params.get("total_amount", 0) or 0)
    num_chunks = int(params.get("num_chunks", 0) or 0)
    interval_seconds = int(params.get("interval_seconds", 0) or 0)
    chunks_executed = int(params.get("chunks_executed", 0) or 0)
    last_chunk_time = int(params.get("last_chunk_time", 0) or 0)
    fee_tier = int(
        params.get(
            "fee_tier",
            _intent_field_default(manifest, intent_name, "fee_tier", 3000),
        ) or 3000
    )
    receiver_field = canonical_swap_receiver_field(manifest, intent_name=intent_name)
    receiver = _resolve_swap_receiver(
        params,
        canonical_field=receiver_field,
        receiver_default=receiver_default,
    )

    min_output_raw = params.get("min_output_per_chunk")
    if min_output_raw not in (None, ""):
        min_output_per_chunk = int(min_output_raw)
    elif slippage_bps is not None and total_amount > 0 and num_chunks > 0:
        chunk_amount = total_amount // num_chunks
        min_output_per_chunk = chunk_amount * (10000 - slippage_bps) // 10000
    else:
        min_output_per_chunk = 0

    return {
        "input_token": params.get("input_token", ""),
        "output_token": params.get("output_token", ""),
        "total_amount": total_amount,
        "num_chunks": num_chunks,
        "interval_seconds": interval_seconds,
        "chunks_executed": chunks_executed,
        "last_chunk_time": last_chunk_time,
        "min_output_per_chunk": min_output_per_chunk,
        "receiver": receiver,
        "receiver_field": receiver_field,
        "fee_tier": fee_tier,
    }


def normalize_rebalance_intent_params(
    params: dict[str, Any],
    *,
    manifest: IntentManifest | None = None,
    intent_name: str = "rebalance",
) -> dict[str, Any]:
    """Normalize rebalance params using manifest defaults when available."""
    threshold_pct = params.get(
        "threshold_pct",
        _intent_field_default(manifest, intent_name, "threshold_pct", 0.0),
    )
    total_value_usd = params.get(
        "total_value_usd",
        _intent_field_default(manifest, intent_name, "total_value_usd", 0.0),
    )
    return {
        "target_allocations": dict(params.get("target_allocations", {}) or {}),
        "current_allocations": dict(params.get("current_allocations", {}) or {}),
        "threshold_pct": float(threshold_pct or 0.0),
        "total_value_usd": float(total_value_usd or 0.0),
        "token_addresses": dict(params.get("token_addresses", {}) or {}),
        "token_decimals": {
            key: int(value)
            for key, value in dict(params.get("token_decimals", {}) or {}).items()
        },
    }


def canonical_swap_receiver_field(
    manifest: IntentManifest | None,
    *,
    intent_name: str = "swap",
) -> str:
    """Return the manifest-preferred receiver-like field name for swap intents."""
    if manifest is None:
        return "receiver"
    intent = manifest.get_intent(intent_name)
    if intent is None:
        return "receiver"
    field_names = {field.name for field in intent.params}
    if "receiver" in field_names:
        return "receiver"
    if "recipient" in field_names:
        return "recipient"
    return "receiver"


def _resolve_swap_receiver(
    params: dict[str, Any],
    *,
    canonical_field: str,
    receiver_default: str,
) -> str:
    if canonical_field == "recipient":
        return (
            params.get("recipient")
            or params.get("receiver")
            or receiver_default
        )
    return (
        params.get("receiver")
        or params.get("recipient")
        or receiver_default
    )


def _intent_field_default(
    manifest: IntentManifest | None,
    intent_name: str,
    field_name: str,
    fallback: Any,
) -> Any:
    if manifest is None:
        return fallback
    intent = manifest.get_intent(intent_name)
    if intent is None:
        return fallback
    field = intent.get_param(field_name)
    if field is None or field.default is None:
        return fallback
    return field.default


# ── Spend-side inference (simulator token seeding) ──────────────────────────


def spend_params(
    manifest: IntentManifest | dict[str, Any] | None,
    intent_function: str,
) -> tuple[str | None, str | None]:
    """Infer ``(token_param, amount_param)`` — the app's spend side.

    The simulator must pre-fund whatever an order spends, or the intent
    reverts in safeTransferFrom and scores 0. Which params those are is the
    APP's business, and the manifest already says: the spend side is a
    user-supplied address paired with a user-supplied amount. ``source=quote``
    (a slippage guard) and ``source=system`` (receiver, permit fields) are
    excluded by construction, as are ``in_signature=False`` computed params.

    Returns ``(None, None)`` when the app declares no UNAMBIGUOUS pair — an
    app with several user-supplied amounts has to say which one it spends,
    and guessing there is exactly the failure this replaces. Callers must
    treat that as "cannot seed" and say so out loud; a silently unseeded order
    scores 0 and looks like a bad solver rather than a manifest gap.
    """
    fns: list[Any]
    if isinstance(manifest, dict):
        fns = manifest.get("intent_functions", []) or []
    elif manifest is not None:
        fns = list(getattr(manifest, "intents", []) or [])
    else:
        return None, None

    for fn in fns:
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
        if name != intent_function:
            continue
        raw = fn.get("params") if isinstance(fn, dict) else getattr(fn, "params", None)
        if isinstance(raw, dict):
            items = list(raw.items())
        else:
            items = [
                (
                    p.get("name") if isinstance(p, dict) else getattr(p, "name", None),
                    p,
                )
                for p in (raw or [])
            ]

        addrs: list[str] = []
        amounts: list[str] = []
        for pname, spec in items:
            if not pname:
                continue
            get = (
                spec.get if isinstance(spec, dict)
                else lambda k, d=None: getattr(spec, k, d)
            )
            if str(get("source", "user")).lower() != "user":
                continue
            if get("in_signature") is False:
                continue
            vt = str(get("type", None) or get("value_type", None) or "").lower()
            if vt.startswith("address"):
                addrs.append(pname)
            elif vt.startswith(("uint", "int")):
                amounts.append(pname)

        if len(amounts) != 1 or not addrs:
            return None, None
        return addrs[0], amounts[0]
    return None, None


def spend_token_balances(
    manifest: IntentManifest | dict[str, Any] | None,
    intent_function: str,
    params: dict[str, Any],
    *,
    default_chain_id: int = 0,
) -> tuple[dict[str, int] | None, str]:
    """``({token: amount}, "ok")`` for the simulator, or ``(None, reason)``.

    The reason is for the caller to LOG — see spend_params on why a silent
    decline is the wrong failure mode.
    """
    tok_p, amt_p = spend_params(manifest, intent_function)
    if not tok_p or not amt_p:
        return None, (
            f"app declares no unambiguous spend pair for intent "
            f"'{intent_function}'"
        )
    token = params.get(tok_p)
    amount = params.get(amt_p)
    if not token or amount is None:
        return None, f"order omits {tok_p}/{amt_p}"
    token = str(token)
    if token.startswith("eip155:"):
        from minotaur_subnet.shared.interop_address import parse_address

        try:
            token = parse_address(token, default_chain_id=default_chain_id).address
        except ValueError:
            return None, f"unparseable interop address in {tok_p}"
    try:
        return {token: int(amount)}, "ok"
    except (TypeError, ValueError):
        return None, f"non-integer {amt_p}"
