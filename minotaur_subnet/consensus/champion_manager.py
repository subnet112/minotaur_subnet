"""Champion consensus manager for closed-round solver certification."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_hash.auto import keccak

from minotaur_subnet.harness.round_store import ChampionApproval, ChampionCertificate
from .eip712 import build_domain_separator
from .protocol_config import ProtocolConfig

logger = logging.getLogger(__name__)

CHAMPION_APPROVAL_TYPEHASH = keccak(
    b"ChampionApproval("
    b"bytes32 roundId,"
    b"bytes32 committeeHash,"
    b"bytes32 incumbentImageId,"
    b"bytes32 candidateSubmissionId,"
    b"bytes32 candidateImageId,"
    b"bytes32 benchmarkPackHash,"
    b"bytes32 shadowCaseLogHash,"
    b"uint256 effectiveEpoch,"
    b"bytes32 commitHash,"
    b"uint256 nonce,"
    b"uint256 deadline"
    b")"
)


# Signed approvals are valid for this long by default; longer than the
# champion-consensus timeout so a slow round doesn't reject sigs that were
# minted near the start.
CHAMPION_APPROVAL_DEADLINE_SECONDS = 3600


@dataclass(frozen=True)
class ChampionProposal:
    """Canonical signable tuple for champion certification.

    commit_hash, nonce, and deadline are part of the EIP-712 signed struct
    (v2). commit_hash binds the git SHA; nonce is per-signer monotonic;
    deadline caps signature lifetime.
    """

    round_id: str
    committee_hash: str | None = None
    incumbent_image_id: str | None = None
    candidate_submission_id: str = ""
    candidate_image_id: str = ""
    benchmark_pack_hash: str | None = None
    shadow_case_log_hash: str | None = None
    effective_epoch: int = 0
    commit_hash: str | None = None
    nonce: int = 0
    deadline: int = 0


@dataclass
class ChampionConsensusResult:
    """Result of a champion certification quorum attempt."""

    reached: bool
    approvals: list[ChampionApproval] = field(default_factory=list)
    quorum: int = 1
    collected: int = 0
    certificate: ChampionCertificate | None = None


class ChampionConsensusManager:
    """Collect validator signatures for a champion certification tuple.

    The validator set and quorum threshold are sourced from a
    ``ProtocolConfig`` instance that reads on-chain state (BT EVM
    ``ValidatorRegistry.getValidators()`` for the set + the configured
    quorum source — typically ``ChampionRegistry.quorumBps()`` since it
    keeps an independent threshold). The manager reads through to the
    config on every operation, so on-chain ``updateValidators`` and
    ``setQuorumBps`` changes propagate without restart on the next
    refresh tick.

    Backwards-compat (tests, manual override): if ``validators`` is
    passed at construction or ``quorum_bps`` is supplied without a
    ``protocol_config``, those pinned values take precedence.
    """

    def __init__(
        self,
        validator_id: str,
        private_key: str,
        *,
        protocol_config: ProtocolConfig | None = None,
        quorum_bps: int | None = None,
        validators: list[str] | None = None,
        timeout: float = 30.0,
        chain_id: int = 31337,
        contract_address: str = "0x" + "00" * 20,
        domain_separator: bytes | None = None,
    ) -> None:
        if protocol_config is None and quorum_bps is None:
            raise ValueError(
                "ChampionConsensusManager requires either protocol_config "
                "(production) or quorum_bps (tests / manual override)",
            )
        self.validator_id = validator_id
        self.private_key = private_key
        self.protocol_config = protocol_config
        # When validators is explicitly passed, it pins the set (tests /
        # local-testnet). When None, the validators property reads through to
        # protocol_config.peers + self.validator_id — picking up newly
        # discovered peers automatically on each refresh tick.
        self._validators_override = validators
        # When quorum_bps is explicitly passed, it pins (tests). Else the
        # property reads through to protocol_config.quorum_bps.
        self._quorum_bps_override = quorum_bps
        self.timeout = timeout
        self.chain_id = chain_id
        self.contract_address = contract_address
        self.domain_separator = domain_separator or build_domain_separator(
            chain_id,
            contract_address,
            name="MinotaurChampionConsensus",
            version="1",
        )
        self._pending: dict[str, _PendingChampionProposal] = {}

    @property
    def quorum_bps(self) -> int:
        """Current champion-consensus quorum threshold in basis points."""
        if self._quorum_bps_override is not None:
            return self._quorum_bps_override
        assert self.protocol_config is not None
        return self.protocol_config.quorum_bps

    @property
    def validators(self) -> list[str]:
        """Current trusted validator set."""
        if self._validators_override is not None:
            return self._validators_override
        if self.protocol_config is None:
            return [self.validator_id]
        return [self.validator_id] + [
            p.evm_address for p in self.protocol_config.peers
        ]

    @property
    def quorum_required(self) -> int:
        """Number of validator approvals needed for certification.

        Reads from ``ProtocolConfig.on_chain_validator_count`` when wired,
        falling back to ``len(self.validators)`` for legacy/test setups
        that pin the validator set directly. See
        ``ConsensusManager.quorum_required`` for the full rationale —
        same fix for the order-consensus path applied here so
        champion-consensus inherits the same jitter-immunity.
        """
        n = 0
        if self.protocol_config is not None:
            n = self.protocol_config.on_chain_validator_count
        if n == 0:
            n = len(self.validators)
        return max(1, (n * self.quorum_bps + 9999) // 10000)

    @property
    def committee_hash(self) -> str:
        """Deterministic hash of the ordered validator committee."""
        ordered = sorted(v.lower() for v in self.validators if v)
        return "0x" + keccak("|".join(ordered).encode("utf-8")).hex()

    def set_validators(self, validators: list[str]) -> None:
        """Pin the validator set (overrides the protocol_config view).

        Used by tests and any caller that wants a fixed set. Production
        code should leave the override unset and let the protocol_config
        refresh loop drive the set.
        """
        cleaned = [validator for validator in validators if validator]
        if not cleaned:
            cleaned = [self.validator_id]
        if cleaned != self.validators:
            self.clear_all_pending()
        self._validators_override = cleaned

    async def propose(self, proposal: ChampionProposal) -> ChampionConsensusResult:
        """Start a new certification quorum attempt for a finalist."""
        approval = self.sign_approval(proposal)
        pending = _PendingChampionProposal(
            proposal=proposal,
            quorum=self.quorum_required,
        )
        pending.add_approval(approval)
        self._pending[proposal.round_id] = pending

        if pending.has_quorum:
            certificate = pending.build_certificate()
            del self._pending[proposal.round_id]
            return ChampionConsensusResult(
                reached=True,
                approvals=pending.sorted_approvals(),
                quorum=pending.quorum,
                collected=len(pending.approvals),
                certificate=certificate,
            )

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            current = self._pending.get(proposal.round_id)
            if current is None:
                return ChampionConsensusResult(reached=False, quorum=pending.quorum)
            if current.has_quorum:
                current = self._pending.pop(proposal.round_id)
                return ChampionConsensusResult(
                    reached=True,
                    approvals=current.sorted_approvals(),
                    quorum=current.quorum,
                    collected=len(current.approvals),
                    certificate=current.build_certificate(),
                )
            await asyncio.sleep(0.05)

        return ChampionConsensusResult(
            reached=False,
            approvals=pending.sorted_approvals(),
            quorum=pending.quorum,
            collected=len(pending.approvals),
        )

    def sign_approval(self, proposal: ChampionProposal) -> ChampionApproval:
        """Sign the champion tuple with this validator's key."""
        signature = sign_champion_approval(
            self.private_key,
            proposal,
            domain_separator=self.domain_separator,
        )
        return ChampionApproval(
            validator_id=self.validator_id,
            round_id=proposal.round_id,
            committee_hash=proposal.committee_hash,
            incumbent_image_id=proposal.incumbent_image_id,
            candidate_submission_id=proposal.candidate_submission_id,
            candidate_image_id=proposal.candidate_image_id,
            benchmark_pack_hash=proposal.benchmark_pack_hash,
            shadow_case_log_hash=proposal.shadow_case_log_hash,
            effective_epoch=proposal.effective_epoch,
            commit_hash=proposal.commit_hash,
            nonce=proposal.nonce,
            deadline=proposal.deadline,
            timestamp=time.time(),
            signature=signature,
        )

    def verify_approval(
        self,
        approval: ChampionApproval,
        proposal: ChampionProposal | None = None,
    ) -> bool:
        """Validate signer membership, tuple equality, and signature."""
        if approval.validator_id not in self.validators:
            return False
        expected = proposal or proposal_from_approval(approval)
        if not _approval_matches_proposal(approval, expected):
            return False
        return verify_champion_approval(
            approval.validator_id,
            approval.signature,
            expected,
            domain_separator=self.domain_separator,
        )

    def receive_approval(
        self,
        approval: ChampionApproval,
    ) -> ChampionConsensusResult | None:
        """Receive a peer validator's approval for a pending round."""
        pending = self._pending.get(approval.round_id)
        if pending is None:
            logger.warning(
                "Received champion approval for unknown round: %s",
                approval.round_id,
            )
            return None
        if not _approval_matches_proposal(approval, pending.proposal):
            logger.warning(
                "Champion approval tuple mismatch for round %s from %s",
                approval.round_id,
                approval.validator_id,
            )
            return None
        if not self.verify_approval(approval, pending.proposal):
            logger.warning(
                "Invalid champion approval signature for round %s from %s",
                approval.round_id,
                approval.validator_id,
            )
            return None

        pending.add_approval(approval)
        if not pending.has_quorum:
            return None
        return ChampionConsensusResult(
            reached=True,
            approvals=pending.sorted_approvals(),
            quorum=pending.quorum,
            collected=len(pending.approvals),
            certificate=pending.build_certificate(),
        )

    def clear_all_pending(self) -> int:
        """Drop all in-flight champion certification attempts."""
        count = len(self._pending)
        if count > 0:
            logger.info("Clearing %d pending champion certification attempts", count)
            self._pending.clear()
        return count

    def prune_expired(self, now: float | None = None) -> list[str]:
        """Remove certification attempts that timed out."""
        now = now or time.time()
        expired: list[str] = []
        for round_id, pending in list(self._pending.items()):
            if now - pending.created_at > self.timeout:
                expired.append(round_id)
                del self._pending[round_id]
        return expired


def proposal_from_approval(approval: ChampionApproval) -> ChampionProposal:
    """Reconstruct a signable proposal tuple from an approval envelope."""
    return ChampionProposal(
        round_id=approval.round_id,
        committee_hash=approval.committee_hash,
        incumbent_image_id=approval.incumbent_image_id,
        candidate_submission_id=approval.candidate_submission_id or "",
        candidate_image_id=approval.candidate_image_id or "",
        benchmark_pack_hash=approval.benchmark_pack_hash,
        shadow_case_log_hash=approval.shadow_case_log_hash,
        effective_epoch=approval.effective_epoch,
        commit_hash=approval.commit_hash,
        nonce=int(approval.nonce or 0),
        deadline=int(approval.deadline or 0),
    )


def sign_champion_approval(
    private_key: str,
    proposal: ChampionProposal,
    *,
    domain_separator: bytes,
) -> str:
    """Sign a champion certification tuple with EIP-712 typed data."""
    digest = _to_typed_data_hash(
        domain_separator,
        hash_champion_approval_struct(proposal),
    )
    signed = Account.unsafe_sign_hash(digest, private_key=private_key)
    return signed.signature.hex()


def verify_champion_approval(
    address: str,
    signature: str,
    proposal: ChampionProposal,
    *,
    domain_separator: bytes,
) -> bool:
    """Verify a champion approval EIP-712 signature."""
    try:
        digest = _to_typed_data_hash(
            domain_separator,
            hash_champion_approval_struct(proposal),
        )
        recovered = Account._recover_hash(
            digest,
            signature=bytes.fromhex(signature.replace("0x", "")),
        )
        return recovered.lower() == address.lower()
    except Exception:
        return False


def hash_champion_approval_struct(proposal: ChampionProposal) -> bytes:
    """Hash the champion approval struct for EIP-712 signing.

    Layout matches ChampionRegistry.sol CHAMPION_APPROVAL_TYPEHASH v2:
    11 fields total (8 legacy + commitHash + nonce + deadline).
    """
    return keccak(abi_encode(
        [
            "bytes32",  # typehash
            "bytes32",  # roundId
            "bytes32",  # committeeHash
            "bytes32",  # incumbentImageId
            "bytes32",  # candidateSubmissionId
            "bytes32",  # candidateImageId
            "bytes32",  # benchmarkPackHash
            "bytes32",  # shadowCaseLogHash
            "uint256",  # effectiveEpoch
            "bytes32",  # commitHash
            "uint256",  # nonce
            "uint256",  # deadline
        ],
        [
            CHAMPION_APPROVAL_TYPEHASH,
            _str_to_bytes32(proposal.round_id),
            _str_to_bytes32(proposal.committee_hash),
            _str_to_bytes32(proposal.incumbent_image_id),
            _str_to_bytes32(proposal.candidate_submission_id),
            _str_to_bytes32(proposal.candidate_image_id),
            _str_to_bytes32(proposal.benchmark_pack_hash),
            _str_to_bytes32(proposal.shadow_case_log_hash),
            int(proposal.effective_epoch),
            _str_to_bytes32(proposal.commit_hash),
            int(proposal.nonce),
            int(proposal.deadline),
        ],
    ))


def _str_to_bytes32(value: str | None) -> bytes:
    raw = (value or "").strip()
    if not raw:
        return b"\x00" * 32
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return keccak(raw.encode("utf-8"))


def _to_typed_data_hash(domain_separator: bytes, struct_hash: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def _approval_matches_proposal(
    approval: ChampionApproval,
    proposal: ChampionProposal,
) -> bool:
    return (
        approval.round_id == proposal.round_id
        and (approval.committee_hash or None) == (proposal.committee_hash or None)
        and (approval.incumbent_image_id or None) == (proposal.incumbent_image_id or None)
        and (approval.candidate_submission_id or "") == proposal.candidate_submission_id
        and (approval.candidate_image_id or "") == proposal.candidate_image_id
        and (approval.benchmark_pack_hash or None) == (proposal.benchmark_pack_hash or None)
        and (approval.shadow_case_log_hash or None) == (proposal.shadow_case_log_hash or None)
        and int(approval.effective_epoch) == int(proposal.effective_epoch)
        and (approval.commit_hash or None) == (proposal.commit_hash or None)
        and int(approval.nonce or 0) == int(proposal.nonce)
        and int(approval.deadline or 0) == int(proposal.deadline)
    )


@dataclass
class _PendingChampionProposal:
    proposal: ChampionProposal
    quorum: int
    approvals: dict[str, ChampionApproval] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def add_approval(self, approval: ChampionApproval) -> None:
        self.approvals[approval.validator_id] = approval

    @property
    def has_quorum(self) -> bool:
        return len(self.approvals) >= self.quorum

    def sorted_approvals(self) -> list[ChampionApproval]:
        return sorted(
            self.approvals.values(),
            key=lambda item: int(item.validator_id.replace("0x", ""), 16),
        )

    def build_certificate(self) -> ChampionCertificate:
        return ChampionCertificate(
            round_id=self.proposal.round_id,
            committee_hash=self.proposal.committee_hash,
            candidate_submission_id=self.proposal.candidate_submission_id,
            candidate_image_id=self.proposal.candidate_image_id,
            incumbent_image_id=self.proposal.incumbent_image_id,
            benchmark_pack_hash=self.proposal.benchmark_pack_hash,
            shadow_case_log_hash=self.proposal.shadow_case_log_hash,
            effective_epoch=self.proposal.effective_epoch,
            quorum_required=self.quorum,
            approvals=self.sorted_approvals(),
        )
