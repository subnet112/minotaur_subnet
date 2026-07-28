"""Plan-set approval — the user's single signature over every leg of a
multi-leg intent.

Mirrors minotaur_contracts EIP712Verifier.PLAN_SET_APPROVAL_TYPEHASH /
PLAN_SET_DOMAIN_SEPARATOR and AppIntentBaseV2.executeLegSigned: the user
signs PlanSetApproval(orderId, planSetHash) ONCE at quote acceptance, where
planSetHash = keccak256(abi.encodePacked(orderedLegPlanHashes)) — the
hashPlan of every forward leg followed by every rollback/revert leg. The
contract then requires each executed leg's plan to be a member of the
signed set, so neither the relayer nor a colluding quorum can substitute a
recovery path the user never agreed to.

The domain is deliberately CHAIN-AGNOSTIC (name+version only): legs execute
on different chains whose app deployments each have their own chainId-bound
DOMAIN_SEPARATOR, and one wallet prompt must cover all of them. Scope
safety comes from orderId + the plan hashes (which bind per-chain targets/
calldata), per-leg legExecuted replay keys, and the per-leg quorum +
min-out gauntlet — not from the domain.

HASH-CONSISTENCY INVARIANT: a leg's hash here must equal the on-chain
hashPlan of the plan the relayer LATER submits. Both sides therefore build
the leg's ExecutionPlan through build_leg_execution_plan() below — any
metadata drift between hash time and submit time breaks set membership and
strands the leg (fail-closed, but a liveness bug). Never construct leg
ExecutionPlans inline in orchestrators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_abi import encode as abi_encode
from eth_hash.auto import keccak

from minotaur_subnet.consensus.eip712 import hash_plan_eip712
from minotaur_subnet.shared.types import ExecutionPlan, LegPlan

# ── Constants (must match minotaur_contracts EIP712Verifier.sol) ────────────

PLAN_SET_APPROVAL_TYPEHASH = keccak(
    b"PlanSetApproval(bytes32 orderId,bytes32 planSetHash)"
)

PLAN_SET_DOMAIN_SEPARATOR = keccak(abi_encode(
    ["bytes32", "bytes32", "bytes32"],
    [
        keccak(b"EIP712Domain(string name,string version)"),
        keccak(b"MinotaurPlanSet"),
        keccak(b"1"),
    ],
))


# ── Canonical leg → ExecutionPlan construction ──────────────────────────────


def build_leg_execution_plan(
    app_id: str,
    order_deadline: int,
    leg: LegPlan,
    is_rollback: bool = False,
) -> ExecutionPlan:
    """The ONE place a LegPlan becomes an ExecutionPlan.

    Used by the MultiLegOrchestrator (forward + rollback), the
    BridgeTracker's post-bridge destination legs, and the plan-set hasher —
    keeping the submitted plan byte-identical to the hashed one (metadata
    key order included; json.dumps preserves insertion order).
    """
    metadata: dict[str, Any] = {
        **leg.metadata,
        "leg_index": leg.leg_index,
        "chain_id": leg.chain_id,
    }
    if is_rollback:
        metadata["is_rollback"] = True
    return ExecutionPlan(
        intent_id=app_id,
        interactions=leg.interactions,
        deadline=int(order_deadline),
        nonce=0,
        metadata=metadata,
    )


def leg_plan_hash(plan: ExecutionPlan) -> bytes:
    """On-chain hashPlan of a leg's ExecutionPlan (bytes32).

    Mirrors relayer/encoder.encode_execution_plan's conversion exactly
    (checksummed target from lowered hex, int value, raw calldata bytes,
    json.dumps(metadata) bytes) then EIP712Verifier.hashPlan via
    hash_plan_eip712.
    """
    import json

    from eth_utils import to_checksum_address

    calls: list[tuple[str, int, bytes]] = []
    for ix in plan.interactions:
        cd = ix.call_data or ""
        cd_hex = cd[2:] if cd.startswith("0x") else cd
        try:
            cd_bytes = bytes.fromhex(cd_hex) if cd_hex else b""
        except ValueError:
            # Non-hex calldata exists only on MockBridgeAdapter placeholder
            # interactions ("0xbridge_mock_..."), which never reach the EVM
            # encoder (mock relayer only). Hash the raw utf-8 bytes so mock
            # e2e flows get deterministic plan sets instead of a crash.
            cd_bytes = cd.encode()
        calls.append((
            to_checksum_address(ix.target.lower()),
            int(ix.value),
            cd_bytes,
        ))
    metadata = json.dumps(plan.metadata).encode() if plan.metadata else b""
    return hash_plan_eip712(calls, plan.deadline, plan.nonce, metadata)


# ── Plan-set computation ─────────────────────────────────────────────────────


@dataclass
class PlanSet:
    """The ordered leg plan hashes the user signs, plus lookup metadata."""
    leg_hashes: list[bytes]                 # forward legs then rollback legs
    plan_set_hash: bytes                    # keccak(concat(leg_hashes))
    position_by_leg_index: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hashes": ["0x" + h.hex() for h in self.leg_hashes],
            "plan_set_hash": "0x" + self.plan_set_hash.hex(),
            "positions": {str(k): v for k, v in self.position_by_leg_index.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanSet":
        hashes = [bytes.fromhex(h[2:]) for h in d.get("hashes", [])]
        return cls(
            leg_hashes=hashes,
            plan_set_hash=bytes.fromhex(d["plan_set_hash"][2:]),
            position_by_leg_index={
                int(k): v for k, v in d.get("positions", {}).items()
            },
        )


def compute_plan_set(
    multi_leg_plan: Any,
    order_deadline: int,
    app_id: str = "",
) -> PlanSet:
    """Hash every leg (forward then rollback) into the signable plan set.

    ``app_id`` only fills ExecutionPlan.intent_id, which the on-chain plan
    encoding never sees — hashes are app_id-independent, so the quote path
    may pass "".
    """
    leg_hashes: list[bytes] = []
    positions: dict[int, int] = {}

    for leg in multi_leg_plan.forward_legs:
        plan = build_leg_execution_plan(app_id, order_deadline, leg)
        positions[leg.leg_index] = len(leg_hashes)
        leg_hashes.append(leg_plan_hash(plan))

    for leg in multi_leg_plan.rollback_legs:
        plan = build_leg_execution_plan(app_id, order_deadline, leg, is_rollback=True)
        positions[leg.leg_index] = len(leg_hashes)
        leg_hashes.append(leg_plan_hash(plan))

    packed = b"".join(leg_hashes)
    return PlanSet(
        leg_hashes=leg_hashes,
        plan_set_hash=keccak(packed),
        position_by_leg_index=positions,
    )


def thread_plan_set_params(
    params: dict[str, Any],
    plan_set: dict[str, Any] | None,
    signature: str,
    leg_index: int,
) -> None:
    """Attach the executeLegSigned inputs to a submit-order's params.

    No-op unless a plan set AND the user's signature are present and the leg
    has a position in the set — the relayer then falls back to the legacy
    executeLeg path (which the contract accepts until
    planSetSignatureRequired is armed). Underscore keys are relayer-internal
    and never reach on-chain intentParams.
    """
    if not plan_set or not signature:
        return
    position = (plan_set.get("positions") or {}).get(str(leg_index))
    if position is None:
        return
    params["_plan_set_hashes"] = plan_set.get("hashes") or []
    params["_plan_set_position"] = position
    params["_plan_set_signature"] = signature


# ── Digest + verification ────────────────────────────────────────────────────


def plan_set_digest(order_id: str, plan_set_hash: bytes) -> bytes:
    """The EIP-712 digest the user's wallet signs.

    ``order_id`` is the Python order-id string, converted to bytes32 the
    same way the relayer encodes IntentOrder.orderId (keccak of utf-8).
    """
    order_id_bytes = keccak(order_id.encode())
    struct_hash = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32"],
        [PLAN_SET_APPROVAL_TYPEHASH, order_id_bytes, plan_set_hash],
    ))
    return keccak(b"\x19\x01" + PLAN_SET_DOMAIN_SEPARATOR + struct_hash)


def sign_plan_set_approval(
    private_key: str,
    order_id: str,
    plan_set_hash: bytes,
) -> bytes:
    """Sign a PlanSetApproval digest (test/tooling helper — in production
    the USER's wallet signs; the platform never holds this key)."""
    from eth_account import Account

    digest = plan_set_digest(order_id, plan_set_hash)
    signed = Account.unsafe_sign_hash(digest, private_key=private_key)
    return signed.signature


def verify_plan_set_signature(
    signer: str,
    order_id: str,
    plan_set_hash: bytes,
    signature_hex: str,
) -> bool:
    """Server-side pre-check that a submitted approval recovers to ``signer``.

    EOA (ECDSA) only — ERC-1271 smart-account signatures can't be verified
    off-chain without a per-chain RPC roundtrip and are accepted optimistically
    here; the contract's SignatureChecker remains the authority either way
    (an invalid one just reverts at execution, funds untouched).
    """
    sig_hex = signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
    try:
        sig = bytearray(bytes.fromhex(sig_hex))
    except ValueError:
        return False
    if len(sig) != 65:
        # Non-standard length — assume ERC-1271, defer to on-chain check.
        return True
    if sig[64] < 27:
        sig[64] += 27

    from eth_account import Account

    digest = plan_set_digest(order_id, plan_set_hash)
    try:
        recovered = Account._recover_hash(digest, signature=bytes(sig))
    except Exception:
        return False
    return recovered.lower() == signer.lower()
