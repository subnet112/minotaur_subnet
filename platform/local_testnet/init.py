"""Bootstrap script for the Minotaur local testnet.

Runs once at startup inside the `init` Docker container. Orchestrates:
  1. Wait for Anvil (critical — needed for contract deployment)
  2. Fund relayer account
  3. Deploy contracts via forge script
  4. (Optional) Wait for subtensor and register subnet/neurons
  5. Write deployed addresses to /config/testnet.env

Bittensor registration is best-effort — the App Intents stack does not
require it. If subtensor is unreachable or registration fails, the init
still succeeds so that the API/relayer/validator can start.

Reuses patterns from:
  - tests/e2e/conftest.py                        (forge script deployment)
  - tests/emulation/fixtures/anvil_forks.py      (anvil_setBalance)
  - tests/emulation/fixtures/local_subtensor.py  (subnet/neuron registration)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [init] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration from environment ──────────────────────────────────────────

SUBTENSOR_URL = os.environ.get("SUBTENSOR_URL", "ws://subtensor:9944")
ANVIL_RPC_URL = os.environ.get("ANVIL_RPC_URL", "http://anvil:8545")
BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "")
BITTENSOR_EVM_RPC_URL = os.environ.get("BITTENSOR_EVM_RPC_URL", "")
DEPLOYER_KEY = os.environ.get(
    "DEPLOYER_PRIVATE_KEY",
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
)
VALIDATORS_CSV = os.environ.get("VALIDATORS", "")
QUORUM_BPS = os.environ.get("QUORUM_BPS", "6666")
SCORE_THRESHOLD = os.environ.get("SCORE_THRESHOLD", "5000")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/testnet.env")
CONTRACTS_DIR = os.path.join(os.path.dirname(__file__), "../../contracts")
DEPLOY_TIMEOUT_SECONDS = int(os.environ.get("DEPLOY_TIMEOUT_SECONDS", "300"))

# Alice's pre-funded account on localnet
ALICE_RAW_SEED = "0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a"
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

NETUID = int(os.environ.get("NETUID", "1"))
VALIDATOR_SPECS: list[tuple[str, str, float]] = [
    ("validator", "default", 5000.0),
    ("validator_peer_1", "default", 4000.0),
    ("validator_peer_2", "default", 3000.0),
]
MINER_SPECS: list[tuple[str, str, float]] = [
    ("miner", "default", 0.0),
]


# ═════════════════════════════════════════════════════════════════════════════
#                       ANVIL / EVM (CRITICAL PATH)
# ═════════════════════════════════════════════════════════════════════════════


def wait_for_rpc(rpc_url: str, name: str = "Anvil", timeout: int = 60) -> None:
    """Poll an RPC endpoint until it responds."""
    logger.info("Waiting for %s at %s ...", name, rpc_url)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if w3.is_connected():
                block = w3.eth.block_number
                chain_id = w3.eth.chain_id
                logger.info("%s ready — chain_id=%d, block=%d", name, chain_id, block)
                return
        except Exception:
            pass
        time.sleep(1)

    raise RuntimeError(f"{name} not ready after {timeout}s")


def _is_anvil(rpc_url: str) -> bool:
    """Check if *rpc_url* points to an Anvil instance (supports cheat codes)."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        w3.provider.make_request("anvil_nodeInfo", [])
        return True
    except Exception:
        return False


def fund_account(rpc_url: str, name: str = "Anvil") -> None:
    """Ensure the relayer/deployer account has ETH on an Anvil fork.

    On real chains (non-Anvil), skips funding and verifies the deployer
    already has sufficient balance.
    """
    from web3 import Web3
    from eth_account import Account

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    deployer = Account.from_key(DEPLOYER_KEY)
    address = deployer.address

    balance = w3.eth.get_balance(address)
    logger.info("[%s] Relayer %s balance: %s ETH", name, address, w3.from_wei(balance, "ether"))

    if _is_anvil(rpc_url):
        if balance < w3.to_wei(1000, "ether"):
            amount_wei = w3.to_wei(10000, "ether")
            w3.provider.make_request("anvil_setBalance", [address, hex(amount_wei)])
            logger.info("[%s] Funded relayer with 10,000 ETH", name)
    else:
        # Real chain — verify sufficient balance, do NOT attempt cheat codes
        min_balance = w3.to_wei(0.005, "ether")
        if balance < min_balance:
            raise RuntimeError(
                f"[{name}] Deployer {address} has insufficient balance "
                f"({w3.from_wei(balance, 'ether')} ETH). "
                f"Fund with at least 0.005 ETH before deploying."
            )
        logger.info("[%s] Real chain — deployer balance OK (%s ETH)", name, w3.from_wei(balance, "ether"))


def deploy_contracts(rpc_url: str, key_suffix: str = "", name: str = "Anvil") -> dict[str, str]:
    """Deploy the test stack via forge script.

    Args:
        rpc_url: The RPC endpoint to deploy to.
        key_suffix: Suffix appended to address keys (e.g. "_BASE").
        name: Human-readable chain name for logging.

    Returns {KEY: value} address dict.
    """
    logger.info("[%s] Deploying contracts via forge script ...", name)

    env = os.environ.copy()
    env["DEPLOYER_PRIVATE_KEY"] = DEPLOYER_KEY
    env["VALIDATORS"] = VALIDATORS_CSV
    env["QUORUM_BPS"] = QUORUM_BPS
    env["SCORE_THRESHOLD"] = SCORE_THRESHOLD

    result = subprocess.run(
        [
            "forge", "script",
            "script/DeployTestStack.s.sol:DeployTestStack",
            "--rpc-url", rpc_url,
            "--broadcast",
        ],
        capture_output=True,
        text=True,
        cwd=os.path.abspath(CONTRACTS_DIR),
        env=env,
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )

    if result.returncode != 0:
        logger.error("[%s] forge stdout: %s", name, result.stdout)
        logger.error("[%s] forge stderr: %s", name, result.stderr)
        raise RuntimeError(f"Forge script failed on {name}")

    # Parse KEY=VALUE lines, apply suffix
    addresses: dict[str, str] = {}
    for line in result.stdout.split("\n"):
        match = re.search(
            r"(\w+_ADDRESS|DOMAIN_SEPARATOR)=(0x[0-9a-fA-F]+)", line,
        )
        if match:
            key = match.group(1) + key_suffix
            addresses[key] = match.group(2)

    logger.info("[%s] Deployed contracts: %s", name, list(addresses.keys()))
    return addresses


# ═════════════════════════════════════════════════════════════════════════════
#                     BITTENSOR (BEST-EFFORT / OPTIONAL)
# ═════════════════════════════════════════════════════════════════════════════


def setup_bittensor() -> bool:
    """Register subnet + neurons on local subtensor. Returns True on success."""
    global NETUID
    try:
        import bittensor as bt
    except ImportError:
        logger.warning("bittensor not installed, skipping subnet setup")
        return False

    # Wait for subtensor
    logger.info("Waiting for subtensor at %s ...", SUBTENSOR_URL)
    deadline = time.time() + 30  # Short timeout — it's optional
    ready = False
    while time.time() < deadline:
        try:
            sub = bt.Subtensor(network=SUBTENSOR_URL)
            if sub.block > 0:
                logger.info("Subtensor ready at block %d", sub.block)
                ready = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not ready:
        logger.warning("Subtensor not reachable, skipping subnet setup")
        return False

    # Register subnet
    try:
        alice = _get_alice_wallet(bt)
        sub = bt.Subtensor(network=SUBTENSOR_URL)
        logger.info("Registering subnet (netuid=%d) ...", NETUID)
        result = sub.register_subnet(wallet=alice)
        if not result.success:
            logger.warning("Subnet registration failed: %s", result)
            logger.warning("Continuing without bittensor setup")
            return False
        NETUID = _resolve_owned_subnet_netuid(sub, owner_ss58=ALICE_SS58)
        os.environ["NETUID"] = str(NETUID)
        logger.info("Subnet registered and resolved to netuid=%d", NETUID)
        _start_subnet_emissions(bt, sub=sub, wallet=alice, netuid=NETUID)
    except Exception as exc:
        logger.warning("Subnet registration error: %s", exc)
        return False

    # Register the subnet owner hotkey as a neuron so local weight emission can
    # route bootstrap burn weights to the owner just like production.
    try:
        _register_owner_neuron(bt)
    except Exception as exc:
        logger.warning("Failed to register owner neuron: %s", exc)

    # Register validator cluster + miner.
    # Names must match the Docker services' WALLET_NAME / HOTKEY_NAME settings.
    for idx, (name, hotkey, stake) in enumerate(VALIDATOR_SPECS):
        try:
            hotkey_ss58 = _register_neuron(bt, name, hotkey, stake)
            os.environ[f"VALIDATOR_{idx}_HOTKEY_SS58"] = hotkey_ss58
            if idx == 0:
                os.environ["VALIDATOR_HOTKEY_SS58"] = hotkey_ss58
        except Exception as exc:
            logger.warning("Failed to register %s: %s", name, exc)

    for name, hotkey, stake in MINER_SPECS:
        try:
            _register_neuron(bt, name, hotkey, stake)
        except Exception as exc:
            logger.warning("Failed to register %s: %s", name, exc)

    return True


def _get_alice_wallet(bt):
    """Create Alice's pre-funded wallet."""
    wallet = bt.Wallet(name="alice_testnet_init")
    wallet.regenerate_coldkey(
        seed=ALICE_RAW_SEED, use_password=False, overwrite=True,
    )
    wallet.regenerate_hotkey(
        seed=ALICE_RAW_SEED, use_password=False, overwrite=True,
    )
    assert wallet.coldkeypub.ss58_address == ALICE_SS58
    return wallet


def _register_neuron(bt, wallet_name: str, hotkey: str, stake: float) -> str:
    """Register a neuron on the local subnet and return its hotkey SS58."""
    logger.info("Registering %s/%s (stake=%.1f) ...", wallet_name, hotkey, stake)
    sub = bt.Subtensor(network=SUBTENSOR_URL)
    alice = _get_alice_wallet(bt)

    wallet = bt.Wallet(name=wallet_name, hotkey=hotkey)
    wallet.create_if_non_existent(
        coldkey_use_password=False, hotkey_use_password=False,
    )
    hotkey_ss58 = wallet.hotkey.ss58_address

    sub.transfer(
        wallet=alice,
        destination_ss58=wallet.coldkeypub.ss58_address,
        amount=bt.Balance.from_tao(10000),
    )

    result = sub.burned_register(wallet=wallet, netuid=NETUID)
    if not result.success:
        raise RuntimeError(f"burned_register failed: {result}")

    if stake > 0:
        result = sub.add_stake(
            wallet=wallet,
            netuid=NETUID,
            hotkey_ss58=hotkey_ss58,
            amount=bt.Balance.from_tao(stake),
        )
        if hasattr(result, "success") and not result.success:
            raise RuntimeError(f"add_stake failed: {result}")

    logger.info("Neuron %s/%s registered", wallet_name, hotkey)
    return hotkey_ss58


def _start_subnet_emissions(bt, *, sub, wallet, netuid: int) -> None:
    """Activate emissions/staking on a newly created local subnet."""
    logger.info("Starting subnet emissions (netuid=%d) ...", netuid)

    start_call = getattr(sub, "start_call", None)
    if callable(start_call):
        result = start_call(
            wallet=wallet,
            netuid=netuid,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
        if isinstance(result, tuple):
            success, message = result
            if not success:
                raise RuntimeError(f"start_call failed: {message}")
        elif hasattr(result, "success") and not result.success:
            raise RuntimeError(f"start_call failed: {result}")
        logger.info("Subnet emissions started via Subtensor.start_call()")
        return

    from bittensor.core.extrinsics.start_call import start_call_extrinsic

    success, message = start_call_extrinsic(
        subtensor=sub,
        wallet=wallet,
        netuid=netuid,
        wait_for_inclusion=True,
        wait_for_finalization=False,
    )
    if not success:
        raise RuntimeError(f"start_call_extrinsic failed: {message}")
    logger.info("Subnet emissions started via start_call_extrinsic()")


def _register_owner_neuron(bt) -> None:
    """Register the subnet owner hotkey on the local subnet if needed."""
    logger.info("Registering subnet owner hotkey for burn routing ...")
    sub = bt.Subtensor(network=SUBTENSOR_URL)
    alice = _get_alice_wallet(bt)

    result = sub.burned_register(wallet=alice, netuid=NETUID)
    if hasattr(result, "success") and result.success:
        logger.info("Subnet owner hotkey registered for burn routing")
        return

    message = str(result).lower()
    if "already" in message and "register" in message:
        logger.info("Subnet owner hotkey already registered")
        return

    raise RuntimeError(f"Failed to register subnet owner hotkey: {result}")


def _resolve_owned_subnet_netuid(sub, *, owner_ss58: str, timeout: int = 15) -> int:
    """Resolve the most recent non-root subnet owned by the given SS58."""
    deadline = time.time() + timeout
    last_seen: list[int] = []

    while time.time() < deadline:
        owned: list[int] = []
        infos = []
        get_all = getattr(sub, "get_all_subnets_info", None)
        all_subnets = getattr(sub, "all_subnets", None)
        try:
            if callable(get_all):
                infos = get_all() or []
            elif callable(all_subnets):
                infos = all_subnets() or []
        except Exception:
            infos = []

        for info in infos:
            try:
                netuid = int(getattr(info, "netuid", -1))
            except Exception:
                continue
            owner = getattr(info, "owner_ss58", "") or getattr(info, "owner", "")
            if owner == owner_ss58 and netuid > 0:
                owned.append(netuid)

        if owned:
            return max(owned)
        last_seen = owned
        time.sleep(1)

    raise RuntimeError(
        f"Failed to resolve subnet owned by {owner_ss58}; visible owned subnets={last_seen}"
    )


# ═════════════════════════════════════════════════════════════════════════════
#                           CONFIG OUTPUT
# ═════════════════════════════════════════════════════════════════════════════


def write_config(addresses: dict[str, str], bt_ok: bool) -> None:
    """Write deployed addresses to /config/testnet.env."""
    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)

    lines = [
        "# Auto-generated by local_testnet init",
        f"# Generated at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        f"ANVIL_RPC_URL={ANVIL_RPC_URL}",
    ]

    if BASE_RPC_URL:
        lines.append(f"BASE_RPC_URL={BASE_RPC_URL}")

    lines += [
        f"SUBTENSOR_URL={SUBTENSOR_URL}",
        f"NETUID={os.environ.get('NETUID', str(NETUID)).strip()}",
        f"BITTENSOR_REGISTERED={'true' if bt_ok else 'false'}",
        f"SUBNET_OWNER_HOTKEY={ALICE_SS58}",
        f"VALIDATOR_HOTKEY_SS58={os.environ.get('VALIDATOR_HOTKEY_SS58', '').strip()}",
        f"VALIDATOR_0_HOTKEY_SS58={os.environ.get('VALIDATOR_0_HOTKEY_SS58', '').strip()}",
        f"VALIDATOR_1_HOTKEY_SS58={os.environ.get('VALIDATOR_1_HOTKEY_SS58', '').strip()}",
        f"VALIDATOR_2_HOTKEY_SS58={os.environ.get('VALIDATOR_2_HOTKEY_SS58', '').strip()}",
        "",
    ]
    for key, value in sorted(addresses.items()):
        lines.append(f"{key}={value}")

    # Ethereum (Anvil) validator registry
    if "REGISTRY_ADDRESS" in addresses:
        lines.append(f"\nVALIDATOR_REGISTRY_31337={addresses['REGISTRY_ADDRESS']}")

    # Base validator registry
    if "REGISTRY_ADDRESS_BASE" in addresses:
        lines.append(f"VALIDATOR_REGISTRY_8453={addresses['REGISTRY_ADDRESS_BASE']}")

    # BT EVM validator registry + app contract
    if "REGISTRY_ADDRESS_BTEVM" in addresses:
        lines.append(f"VALIDATOR_REGISTRY_964={addresses['REGISTRY_ADDRESS_BTEVM']}")
    if "CHAMPION_REGISTRY_ADDRESS_BTEVM" in addresses:
        lines.append(f"CHAMPION_REGISTRY_964={addresses['CHAMPION_REGISTRY_ADDRESS_BTEVM']}")
        lines.append("CHAMPION_CONSENSUS_CHAIN_ID=964")
    if "DEX_AGGREGATOR_ADDRESS_BTEVM" in addresses:
        lines.append(f"APP_INTENT_BASE_964={addresses['DEX_AGGREGATOR_ADDRESS_BTEVM']}")

    if BITTENSOR_EVM_RPC_URL:
        lines.append(f"BITTENSOR_EVM_RPC_URL={BITTENSOR_EVM_RPC_URL}")

    if "RELAYER_ADDRESS" in addresses:
        lines.append(f"RELAYER_WALLET={addresses['RELAYER_ADDRESS']}")

    content = "\n".join(lines) + "\n"

    with open(CONFIG_PATH, "w") as f:
        f.write(content)

    logger.info("Config written to %s", CONFIG_PATH)
    logger.info("Contents:\n%s", content)


# ═════════════════════════════════════════════════════════════════════════════
#                                MAIN
# ═════════════════════════════════════════════════════════════════════════════


def main() -> None:
    logger.info("=" * 60)
    logger.info("Minotaur Local Testnet — Init")
    logger.info("=" * 60)

    # Critical path: Ethereum mainnet fork (Anvil)
    wait_for_rpc(ANVIL_RPC_URL, name="Anvil-ETH")
    fund_account(ANVIL_RPC_URL, name="Anvil-ETH")
    addresses = deploy_contracts(ANVIL_RPC_URL, name="Anvil-ETH")

    # Critical path: Base chain (Anvil fork or real mainnet)
    if BASE_RPC_URL:
        base_name = "Base-Mainnet" if not _is_anvil(BASE_RPC_URL) else "Anvil-Base"
        wait_for_rpc(BASE_RPC_URL, name=base_name)
        fund_account(BASE_RPC_URL, name=base_name)
        base_addresses = deploy_contracts(
            BASE_RPC_URL, key_suffix="_BASE", name=base_name,
        )
        addresses.update(base_addresses)
    else:
        logger.info("BASE_RPC_URL not set, skipping Base chain setup")

    # Critical path: Bittensor EVM chain (Anvil fork of BT EVM mainnet)
    if BITTENSOR_EVM_RPC_URL:
        btevm_name = "BT-EVM-Mainnet" if not _is_anvil(BITTENSOR_EVM_RPC_URL) else "Anvil-BTEVM"
        wait_for_rpc(BITTENSOR_EVM_RPC_URL, name=btevm_name)
        fund_account(BITTENSOR_EVM_RPC_URL, name=btevm_name)
        btevm_addresses = deploy_contracts(
            BITTENSOR_EVM_RPC_URL, key_suffix="_BTEVM", name=btevm_name,
        )
        addresses.update(btevm_addresses)
    else:
        logger.info("BITTENSOR_EVM_RPC_URL not set, skipping BT EVM setup")

    # Best-effort: Bittensor subnet registration
    bt_ok = setup_bittensor()
    if not bt_ok:
        logger.info("Bittensor setup skipped/failed (non-critical)")

    # Write config
    write_config(addresses, bt_ok)

    logger.info("=" * 60)
    logger.info("Init complete — testnet is ready!")
    logger.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.error("Init failed: %s", exc, exc_info=True)
        sys.exit(1)
