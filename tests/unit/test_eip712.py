"""Unit tests for EIP-712 typed-data signing utilities.

Verifies sign/verify roundtrips, deterministic hashing, and domain separator
computation. These tests run without Anvil — pure Python crypto.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from eth_hash.auto import keccak
from eth_abi import encode as abi_encode
from eth_account import Account

from minotaur_subnet.consensus.eip712 import (
    INTENT_ORDER_TYPEHASH,
    PLAN_APPROVAL_TYPEHASH,
    EIP712_DOMAIN_TYPEHASH,
    build_domain_separator,
    hash_plan_eip712,
    hash_order_struct,
    sign_user_order,
    hash_plan_approval_struct,
    sign_plan_approval_eip712,
    verify_plan_approval_eip712,
    address_from_key,
    _to_typed_data_hash,
)


# ── Test keys ────────────────────────────────────────────────────────────────

KEY_1 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # Anvil #0
KEY_2 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"  # Anvil #1
KEY_3 = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"  # Anvil #2

ADDR_1 = address_from_key(KEY_1)
ADDR_2 = address_from_key(KEY_2)
ADDR_3 = address_from_key(KEY_3)

# Deterministic contract address for tests
CONTRACT = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
CHAIN_ID = 31337


@pytest.fixture
def domain():
    return build_domain_separator(CHAIN_ID, CONTRACT)


# ── Type hash tests ──────────────────────────────────────────────────────────


class TestTypeHashes:
    def test_intent_order_typehash_is_bytes32(self):
        assert len(INTENT_ORDER_TYPEHASH) == 32

    def test_plan_approval_typehash_is_bytes32(self):
        assert len(PLAN_APPROVAL_TYPEHASH) == 32

    def test_domain_typehash_is_bytes32(self):
        assert len(EIP712_DOMAIN_TYPEHASH) == 32

    def test_typehashes_are_distinct(self):
        assert INTENT_ORDER_TYPEHASH != PLAN_APPROVAL_TYPEHASH
        assert INTENT_ORDER_TYPEHASH != EIP712_DOMAIN_TYPEHASH


# ── Domain separator tests ──────────────────────────────────────────────────


class TestDomainSeparator:
    def test_deterministic(self):
        d1 = build_domain_separator(CHAIN_ID, CONTRACT)
        d2 = build_domain_separator(CHAIN_ID, CONTRACT)
        assert d1 == d2

    def test_length(self, domain):
        assert len(domain) == 32

    def test_different_chain_different_domain(self):
        d1 = build_domain_separator(1, CONTRACT)
        d2 = build_domain_separator(31337, CONTRACT)
        assert d1 != d2

    def test_different_address_different_domain(self):
        d1 = build_domain_separator(CHAIN_ID, CONTRACT)
        d2 = build_domain_separator(CHAIN_ID, "0x" + "ab" * 20)
        assert d1 != d2

    def test_matches_manual_computation(self):
        """Verify our domain matches the Solidity constructor formula."""
        expected = keccak(abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                EIP712_DOMAIN_TYPEHASH,
                keccak(b"MinotaurAppIntent"),
                keccak(b"1"),
                CHAIN_ID,
                CONTRACT,
            ],
        ))
        assert build_domain_separator(CHAIN_ID, CONTRACT) == expected


# ── Plan hash tests ──────────────────────────────────────────────────────────


class TestHashPlan:
    def test_deterministic(self):
        calls = [(ADDR_1, 0, b"\xde\xad\xbe\xef")]
        h1 = hash_plan_eip712(calls, 1700000000, 1, b"")
        h2 = hash_plan_eip712(calls, 1700000000, 1, b"")
        assert h1 == h2
        assert len(h1) == 32

    def test_different_calls_different_hash(self):
        h1 = hash_plan_eip712([(ADDR_1, 0, b"\xaa")], 1000, 0)
        h2 = hash_plan_eip712([(ADDR_2, 0, b"\xaa")], 1000, 0)
        assert h1 != h2

    def test_different_deadline_different_hash(self):
        calls = [(ADDR_1, 0, b"")]
        h1 = hash_plan_eip712(calls, 1000, 0)
        h2 = hash_plan_eip712(calls, 2000, 0)
        assert h1 != h2

    def test_different_nonce_different_hash(self):
        calls = [(ADDR_1, 0, b"")]
        h1 = hash_plan_eip712(calls, 1000, 0)
        h2 = hash_plan_eip712(calls, 1000, 1)
        assert h1 != h2

    def test_different_metadata_different_hash(self):
        calls = [(ADDR_1, 0, b"")]
        h1 = hash_plan_eip712(calls, 1000, 0, b"")
        h2 = hash_plan_eip712(calls, 1000, 0, b"meta")
        assert h1 != h2

    def test_multiple_calls(self):
        calls = [
            (ADDR_1, 0, b"\xaa"),
            (ADDR_2, 100, b"\xbb\xcc"),
        ]
        h = hash_plan_eip712(calls, 1700000000, 1)
        assert len(h) == 32

    def test_empty_calldata(self):
        h = hash_plan_eip712([(ADDR_1, 0, b"")], 1000, 0)
        assert len(h) == 32

    def test_matches_manual_solidity_formula(self):
        """Verify step-by-step against the Solidity hashPlan algorithm."""
        target = ADDR_1
        value = 0
        call_data = b"\xde\xad\xbe\xef"
        deadline = 1700000000
        nonce = 1
        metadata = b""

        # Step 1: per-call hash
        call_hash = keccak(abi_encode(
            ["address", "uint256", "bytes32"],
            [target, value, keccak(call_data)],
        ))

        # Step 2: packed call hashes (single call = just the hash itself)
        packed = call_hash

        # Step 3: final hash
        expected = keccak(abi_encode(
            ["bytes32", "uint256", "uint256", "bytes32"],
            [keccak(packed), deadline, nonce, keccak(metadata)],
        ))

        assert hash_plan_eip712([(target, value, call_data)], deadline, nonce, metadata) == expected


# ── Order struct hash tests ──────────────────────────────────────────────────


class TestHashOrderStruct:
    def test_deterministic(self):
        order_id = b"\x01" * 32
        selector = bytes.fromhex("12345678")
        params = b"\xaa\xbb"

        h1 = hash_order_struct(
            order_id, CONTRACT, selector, params, ADDR_1,
            CHAIN_ID, 2000000000, 0, False, 1, 0,
        )
        h2 = hash_order_struct(
            order_id, CONTRACT, selector, params, ADDR_1,
            CHAIN_ID, 2000000000, 0, False, 1, 0,
        )
        assert h1 == h2
        assert len(h1) == 32

    def test_different_order_id(self):
        selector = bytes.fromhex("12345678")
        h1 = hash_order_struct(b"\x01" * 32, CONTRACT, selector, b"", ADDR_1, 1, 1000, 0, False, 1, 0)
        h2 = hash_order_struct(b"\x02" * 32, CONTRACT, selector, b"", ADDR_1, 1, 1000, 0, False, 1, 0)
        assert h1 != h2


# ── User order signing tests ────────────────────────────────────────────────


class TestSignUserOrder:
    def test_sign_produces_65_bytes(self, domain):
        sig = sign_user_order(
            KEY_1,
            order_id=b"\x01" * 32,
            app=CONTRACT,
            intent_selector=bytes.fromhex("12345678"),
            intent_params=b"\xaa\xbb",
            submitted_by=ADDR_1,
            chain_id=CHAIN_ID,
            deadline=2000000000,
            nonce=0,
            perpetual=False,
            max_executions=1,
            cooldown=0,
            domain_separator=domain,
        )
        assert len(sig) == 65

    def test_sign_recovers_to_correct_address(self, domain):
        order_id = b"\x01" * 32
        selector = bytes.fromhex("12345678")
        params = b"\xaa\xbb"

        sig = sign_user_order(
            KEY_1, order_id, CONTRACT, selector, params, ADDR_1,
            CHAIN_ID, 2000000000, 0, False, 1, 0, domain,
        )

        # Recover manually
        struct_hash = hash_order_struct(
            order_id, CONTRACT, selector, params, ADDR_1,
            CHAIN_ID, 2000000000, 0, False, 1, 0,
        )
        digest = _to_typed_data_hash(domain, struct_hash)
        recovered = Account._recover_hash(digest, signature=sig)
        assert recovered.lower() == ADDR_1.lower()


# ── Validator approval signing tests ─────────────────────────────────────────


class TestPlanApprovalSigning:
    def test_sign_produces_65_bytes(self, domain):
        sig = sign_plan_approval_eip712(
            KEY_1, b"\x01" * 32, b"\x02" * 32, 5000, domain,
        )
        assert len(sig) == 65

    def test_verify_roundtrip(self, domain):
        order_id = b"\x01" * 32
        plan_hash = b"\x02" * 32
        score_bps = 5000

        sig = sign_plan_approval_eip712(KEY_1, order_id, plan_hash, score_bps, domain)
        assert verify_plan_approval_eip712(ADDR_1, sig, order_id, plan_hash, score_bps, domain)

    def test_verify_wrong_address_fails(self, domain):
        order_id = b"\x01" * 32
        plan_hash = b"\x02" * 32
        score_bps = 5000

        sig = sign_plan_approval_eip712(KEY_1, order_id, plan_hash, score_bps, domain)
        assert not verify_plan_approval_eip712(ADDR_2, sig, order_id, plan_hash, score_bps, domain)

    def test_verify_wrong_score_fails(self, domain):
        order_id = b"\x01" * 32
        plan_hash = b"\x02" * 32

        sig = sign_plan_approval_eip712(KEY_1, order_id, plan_hash, 5000, domain)
        assert not verify_plan_approval_eip712(ADDR_1, sig, order_id, plan_hash, 6000, domain)

    def test_verify_wrong_plan_hash_fails(self, domain):
        order_id = b"\x01" * 32

        sig = sign_plan_approval_eip712(KEY_1, order_id, b"\x02" * 32, 5000, domain)
        assert not verify_plan_approval_eip712(ADDR_1, sig, order_id, b"\x03" * 32, 5000, domain)

    def test_different_validators_different_sigs(self, domain):
        order_id = b"\x01" * 32
        plan_hash = b"\x02" * 32

        sig1 = sign_plan_approval_eip712(KEY_1, order_id, plan_hash, 5000, domain)
        sig2 = sign_plan_approval_eip712(KEY_2, order_id, plan_hash, 5000, domain)
        assert sig1 != sig2

    def test_multiple_validators_all_verify(self, domain):
        order_id = b"\x01" * 32
        plan_hash = b"\x02" * 32
        score_bps = 5000

        for key, addr in [(KEY_1, ADDR_1), (KEY_2, ADDR_2), (KEY_3, ADDR_3)]:
            sig = sign_plan_approval_eip712(key, order_id, plan_hash, score_bps, domain)
            assert verify_plan_approval_eip712(addr, sig, order_id, plan_hash, score_bps, domain)


# ── Address helper tests ─────────────────────────────────────────────────────


class TestAddressFromKey:
    def test_anvil_account_0(self):
        addr = address_from_key(KEY_1)
        assert addr == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

    def test_anvil_account_1(self):
        addr = address_from_key(KEY_2)
        assert addr == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

    def test_anvil_account_2(self):
        addr = address_from_key(KEY_3)
        assert addr == "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"


# ── WAL-6: Lit wallet EIP-712 order signing ─────────────────────────────────


class TestLitWalletEip712Signing:
    """Test sign_eip712_order on LitMpcWallet using local fallback (WAL-6)."""

    @pytest.fixture
    def wallet(self, tmp_path, monkeypatch):
        """Create a LitMpcWallet that falls back to local."""
        # LocalWalletManager refuses to start without an explicit passphrase
        # (no default fallback for security). Set a test value so the
        # fallback path used here can run.
        monkeypatch.setenv("APP_INTENTS_WALLET_PASSPHRASE", "test-only-passphrase")
        from minotaur_subnet.wallet.lit_wallet import LitMpcWallet
        wallet = LitMpcWallet(
            bridge_url="http://localhost:99999",  # unreachable → fallback
            allow_fallback=True,
        )
        return wallet

    @pytest.fixture
    def local_address(self, wallet):
        """Create a local wallet and return its address."""
        import asyncio
        info = asyncio.run(wallet.create_wallet(chain_ids=[CHAIN_ID]))
        return info.address

    def test_sign_eip712_order_returns_hex(self, wallet, local_address):
        """sign_eip712_order produces a hex signature string."""
        import asyncio

        sig_hex = asyncio.run(
            wallet.sign_eip712_order(
                address=local_address,
                order_id=b"\x01" * 32,
                app=CONTRACT,
                intent_selector=bytes.fromhex("12345678"),
                intent_params=b"\xaa\xbb",
                submitted_by=local_address,
                chain_id=CHAIN_ID,
                deadline=2000000000,
                nonce=0,
                perpetual=False,
                max_executions=1,
                cooldown=0,
                contract_address=CONTRACT,
            )
        )

        # Should be a hex string representing 65 bytes
        sig_bytes = bytes.fromhex(sig_hex.replace("0x", ""))
        assert len(sig_bytes) == 65

    def test_sign_eip712_order_recovers_correctly(self, wallet, local_address):
        """Signature produced by sign_eip712_order recovers to the wallet address."""
        import asyncio

        order_id = b"\x01" * 32
        selector = bytes.fromhex("12345678")
        params = b"\xaa\xbb"

        sig_hex = asyncio.run(
            wallet.sign_eip712_order(
                address=local_address,
                order_id=order_id,
                app=CONTRACT,
                intent_selector=selector,
                intent_params=params,
                submitted_by=local_address,
                chain_id=CHAIN_ID,
                deadline=2000000000,
                nonce=0,
                perpetual=False,
                max_executions=1,
                cooldown=0,
                contract_address=CONTRACT,
            )
        )

        sig_bytes = bytes.fromhex(sig_hex.replace("0x", ""))

        # Manually compute the digest and recover
        struct_hash = hash_order_struct(
            order_id, CONTRACT, selector, params, local_address,
            CHAIN_ID, 2000000000, 0, False, 1, 0,
        )
        domain_sep = build_domain_separator(CHAIN_ID, CONTRACT)
        digest = _to_typed_data_hash(domain_sep, struct_hash)
        recovered = Account._recover_hash(digest, signature=sig_bytes)
        assert recovered.lower() == local_address.lower()

    def test_sign_eip712_order_matches_sign_user_order(self, wallet, local_address):
        """Lit wallet (fallback) produces the same signature as sign_user_order with the same key."""
        import asyncio

        order_id = b"\x01" * 32
        selector = bytes.fromhex("12345678")
        params = b"\xaa\xbb"

        sig_hex = asyncio.run(
            wallet.sign_eip712_order(
                address=local_address,
                order_id=order_id,
                app=CONTRACT,
                intent_selector=selector,
                intent_params=params,
                submitted_by=local_address,
                chain_id=CHAIN_ID,
                deadline=2000000000,
                nonce=0,
                perpetual=False,
                max_executions=1,
                cooldown=0,
                contract_address=CONTRACT,
            )
        )

        # Get the private key from fallback manager and sign directly
        mgr = wallet._get_fallback()
        acct = mgr._get_account(local_address)
        domain = build_domain_separator(CHAIN_ID, CONTRACT)
        direct_sig = sign_user_order(
            acct.key.hex(), order_id, CONTRACT, selector, params,
            local_address, CHAIN_ID, 2000000000, 0, False, 1, 0, domain,
        )

        sig_bytes = bytes.fromhex(sig_hex.replace("0x", ""))
        assert sig_bytes == direct_sig


# ── WAL-2: ERC-20 approve and ERC-2612 permit ──────────────────────────────


class TestBuildApproveCalldata:
    """Test build_approve_calldata (WAL-2)."""

    def test_approve_calldata_starts_with_selector(self):
        from minotaur_subnet.blockchain.tokens import build_approve_calldata
        calldata = build_approve_calldata(ADDR_1, 1000)

        # approve(address,uint256) selector = keccak256("approve(address,uint256)")[:4]
        expected_selector = keccak(b"approve(address,uint256)")[:4]
        assert calldata[:4] == expected_selector

    def test_approve_calldata_length(self):
        from minotaur_subnet.blockchain.tokens import build_approve_calldata
        calldata = build_approve_calldata(ADDR_1, 1000)
        # 4 bytes selector + 32 bytes address + 32 bytes amount
        assert len(calldata) == 4 + 64

    def test_approve_calldata_encodes_amount(self):
        from minotaur_subnet.blockchain.tokens import build_approve_calldata
        amount = 2**256 - 1  # max approval
        calldata = build_approve_calldata(ADDR_1, amount)
        # Last 32 bytes should be all 0xff
        assert calldata[-32:] == b"\xff" * 32

    def test_approve_calldata_encodes_spender(self):
        from minotaur_subnet.blockchain.tokens import build_approve_calldata
        calldata = build_approve_calldata(ADDR_1, 0)
        # Address is in bytes 4:36 (left-padded to 32 bytes)
        addr_bytes = calldata[4:36]
        assert addr_bytes[-20:] == bytes.fromhex(ADDR_1[2:].lower())


class TestBuildPermitSignature:
    """Test build_permit_signature (WAL-2)."""

    def test_permit_returns_v_r_s(self):
        from minotaur_subnet.blockchain.tokens import build_permit_signature
        v, r, s = build_permit_signature(
            private_key=KEY_1,
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token_name="USD Coin",
            owner=ADDR_1,
            spender=CONTRACT,
            value=1000_000_000,
            nonce=0,
            deadline=2000000000,
            chain_id=1,
        )
        assert v in (27, 28)
        assert len(r) == 32
        assert len(s) == 32

    def test_permit_signature_recovers_correctly(self):
        from minotaur_subnet.blockchain.tokens import build_permit_signature
        from eth_abi import encode as abi_encode
        from web3 import Web3

        token_addr = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        token_name = "USD Coin"
        spender = CONTRACT
        value = 1000_000_000
        nonce = 0
        deadline = 2000000000
        chain_id = 1

        v, r, s = build_permit_signature(
            private_key=KEY_1,
            token_address=token_addr,
            token_name=token_name,
            owner=ADDR_1,
            spender=spender,
            value=value,
            nonce=nonce,
            deadline=deadline,
            chain_id=chain_id,
        )

        # Recompute digest manually
        domain_typehash = keccak(
            b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        domain_sep = keccak(abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                domain_typehash,
                keccak(token_name.encode()),
                keccak(b"1"),
                chain_id,
                Web3.to_checksum_address(token_addr),
            ],
        ))
        permit_typehash = keccak(
            b"Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
        )
        struct_hash = keccak(abi_encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256"],
            [permit_typehash, ADDR_1, spender, value, nonce, deadline],
        ))
        digest = keccak(b"\x19\x01" + domain_sep + struct_hash)

        # Reconstruct signature and recover
        sig = r + s + v.to_bytes(1, "big")
        recovered = Account._recover_hash(digest, signature=sig)
        assert recovered.lower() == ADDR_1.lower()

    def test_permit_different_nonce_different_sig(self):
        from minotaur_subnet.blockchain.tokens import build_permit_signature
        _, r1, s1 = build_permit_signature(
            KEY_1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USD Coin", ADDR_1, CONTRACT, 1000, 0, 2000000000, 1,
        )
        _, r2, s2 = build_permit_signature(
            KEY_1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USD Coin", ADDR_1, CONTRACT, 1000, 1, 2000000000, 1,
        )
        assert (r1, s1) != (r2, s2)
