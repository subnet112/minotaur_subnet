"""Events-based Bittensor Validator - fetch events, score miners, emit weights (async)."""
import os
import sys
import asyncio
import argparse
import signal
from enum import Enum
from typing import Dict, Optional

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip .env loading

import bittensor as bt

from .exceptions import ConfigurationException
from .aggregator_client import AggregatorClient
from .simulator import OrderSimulator
from .state_store import StateStore
from .bittensor_validator import BittensorValidator
from .mock_validator import MockValidator


class LogLevel(Enum):
    """Log level enumeration for structured logging."""
    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Validator:
    """Events-based validator: window planning + events ingest + on-chain constrained weights."""
    
    def __init__(self):
        self.config = self.get_config()
        self._validate_config()
        
        # Setup logging with INFO level
        bt.logging(config=self.config, logging_dir=self.config.full_path)
        
        # Force INFO level if LOGURU_LEVEL is set
        log_level = os.getenv("LOGURU_LEVEL", "INFO").upper()
        if log_level == "DEBUG":
            bt.logging.set_debug()
        elif log_level == "TRACE":
            bt.logging.set_trace()
        # INFO is default, WARNING would be set_warning()
        
        self._log("Starting events-based validator (compute weights locally)", LogLevel.INFO, prefix="INIT")
        self._log(f"Log level: {log_level}", LogLevel.INFO, prefix="INIT")
        
        # Initialize components based on mode
        if self.config.validator_mode == "mock":
            # Mock mode - skip Bittensor components
            self._log("Mock mode: Skipping Bittensor initialization", LogLevel.INFO, prefix="INIT")
            self.wallet = None
            self.subtensor = None
            self._init_mock_mode()
        else:
            # Bittensor mode - initialize full components
            self._log("Initializing Bittensor components", LogLevel.INFO, prefix="INIT")
            self.wallet = bt.Wallet(
                name=getattr(self.config.wallet, "name", "default"),
                hotkey=getattr(self.config.wallet, "hotkey", "default"),
                path=getattr(self.config.wallet, "path", "~/.bittensor/wallets"),
            )
            self.subtensor = bt.Subtensor(
                network=getattr(self.config.subtensor, "network", "finney"),
                config=self.config,
            )
            
            self._log(f"Wallet: {self.wallet.hotkey.ss58_address}", LogLevel.INFO, prefix="WALLET")
            self._log(f"Network: {self.subtensor.network}", LogLevel.INFO, prefix="CONFIG", suffix=f"netuid={self.config.netuid}")

        # Initialize state store (needed for tracking last weight block)
        self._state_store = StateStore(base_dir=self.config.full_path)

        # Initialize state store for tracking last weight block (used in both modes)
        if not hasattr(self, "_state_store"):
            self._state_store = StateStore(base_dir=self.config.full_path)
        self._last_weight_block = self._state_store.get_last_weight_block()

        self._log("Validator initialized successfully", LogLevel.SUCCESS, prefix="INIT")

    def _init_mock_mode(self) -> None:
        """Initialize mock mode components (no Bittensor dependencies)."""
        from neurons.mock_validator import MockValidator

        self._log("Initializing mock validator", LogLevel.INFO, prefix="INIT")

        # Create mock validator
        # validator_id will be auto-generated if not provided via VALIDATOR_ID env var
        # Ensure simulator_docker_image has a valid value
        simulator_docker_image = getattr(self.config, "simulator_docker_image", None)
        if not simulator_docker_image:
            simulator_docker_image = "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"
            self._log(f"Using default simulator docker image: {simulator_docker_image}", LogLevel.INFO, prefix="INIT")
        
        # Use VALIDATOR_API_KEY for validator-specific endpoints (required)
        validator_api_key = getattr(self.config, "validator_api_key", None)
        if not validator_api_key:
            raise ValueError("VALIDATOR_API_KEY is required for validator endpoints. Set it via --validator.api_key or VALIDATOR_API_KEY environment variable.")
        
        self._mock_validator = MockValidator(
            aggregator_url=self.config.aggregator_url,
            validator_api_key=validator_api_key,
            simulator_rpc_url=getattr(self.config, "simulator_rpc_url", None),
            simulator_docker_image=simulator_docker_image,
            validator_id=getattr(self.config, "validator_id", None),
            burn_percentage=getattr(self.config, "burn_percentage", 0.0),
            creator_miner_id=getattr(self.config, "creator_miner_id", None),
            validation_interval_seconds=getattr(self.config, "poll_seconds", 5),
            max_concurrent_simulations=getattr(self.config, "simulator_max_concurrent", 5),
            logger=bt.logging,  # Use bittensor logging for consistent SUCCESS level support
        )

        # Initialize state store for mock mode
        self._state_store = StateStore(base_dir=self.config.full_path)

        self._log("Mock validator initialized successfully", LogLevel.SUCCESS, prefix="INIT")

    def _log(self, message: str, level: LogLevel = LogLevel.INFO, prefix: str = "", suffix: str = ""):
        """Unified logging with context for consistent formatting.
        
        Args:
            message: The log message
            level: Log level (from LogLevel enum)
            prefix: Optional prefix tag (e.g., "INIT", "CHAIN", "EVENTS")
            suffix: Optional suffix context (e.g., "epoch=5")
        """
        log_methods = {
            LogLevel.TRACE: bt.logging.trace,
            LogLevel.DEBUG: bt.logging.debug,
            LogLevel.INFO: bt.logging.info,
            LogLevel.SUCCESS: bt.logging.success,
            LogLevel.WARNING: bt.logging.warning,
            LogLevel.ERROR: bt.logging.error,
            LogLevel.CRITICAL: bt.logging.critical,
        }
        log_fn = log_methods.get(level, bt.logging.info)
        log_fn(message, prefix=prefix, suffix=suffix)
    
    def get_config(self):
        """Get validator configuration from args and environment."""
        parser = argparse.ArgumentParser()

        # Wallet arguments (bittensor v10+ no longer provides add_args)
        parser.add_argument("--wallet.name", type=str, default="default", help="Wallet name")
        parser.add_argument("--wallet.hotkey", type=str, default="default", help="Hotkey name")
        parser.add_argument("--wallet.path", type=str, default="~/.bittensor/wallets", help="Wallet path")
        
        # Subtensor arguments
        parser.add_argument("--subtensor.network", type=str, default="finney", help="Bittensor network (finney, test, local)")
        parser.add_argument("--subtensor.chain_endpoint", type=str, default=None, help="Chain endpoint URL (optional)")
        
        # Logging arguments
        parser.add_argument("--logging.debug", action="store_true", help="Enable debug logging")
        parser.add_argument("--logging.trace", action="store_true", help="Enable trace logging")
        parser.add_argument("--logging.logging_dir", type=str, default="~/.bittensor/miners", help="Logging directory")

        # Validator-specific additions (not provided by Bittensor)
        # Aggregator API
        parser.add_argument("--aggregator.url", type=str, default="http://localhost:4100", help="Aggregator API base URL")
        parser.add_argument("--aggregator.api_key", type=str, default=None, help="Aggregator API key (general)")
        parser.add_argument("--validator.api_key", type=str, default=None, help="Validator API key for validator-specific endpoints (required)")
        parser.add_argument("--aggregator.timeout", type=int, default=10, help="Aggregator API request timeout")
        parser.add_argument("--aggregator.verify_ssl", type=int, default=1, help="Verify TLS for aggregator API (1/0)")
        parser.add_argument("--aggregator.max_retries", type=int, default=3, help="HTTP max retries to aggregator API")
        parser.add_argument("--aggregator.backoff_seconds", type=float, default=0.5, help="HTTP backoff factor (seconds)")
        parser.add_argument("--aggregator.page_limit", type=int, default=500, help="Max events per page when fetching")
        # Finalization / tempo gating
        parser.add_argument("--finalization.buffer_blocks", type=int, default=6, help="Blocks after epoch end to wait before processing")
        # Validation parameters
        parser.add_argument("--validation.default_ttl_ms", type=int, default=1000, help="Default solver response TTL (ms)")
        parser.add_argument("--validation.max_response_latency_ms", type=int, default=1500, help="Hard cap on response latency (ms)")
        parser.add_argument("--validation.max_clock_skew_seconds", type=int, default=1, help="Allowed negative latency window (s)")
        # Burn allocation
        parser.add_argument("--burn_percentage", type=float, default=0.0, help="Fraction of emissions to allocate to creator hotkey for burning (0.0-1.0)")
        parser.add_argument("--creator.miner_id", type=str, default=None, help="Creator miner ID (SS58) for burn allocation. In Bittensor mode, defaults to UID 0 hotkey. In mock mode, must be provided manually if burn_percentage > 0")
        # Simulator settings
        parser.add_argument("--simulator.rpc_url", type=str, default=None, help="Ethereum RPC URL for order simulation")
        parser.add_argument("--simulator.docker_image", type=str, default="mino-simulation", help="Docker image for order simulator")
        parser.add_argument("--simulator.max_concurrent", type=int, default=5, help="Maximum number of concurrent simulations (default: 5)")
        # Validator identity
        parser.add_argument("--validator.id", type=str, default=None, help="Unique validator ID for order filtering (defaults to hotkey)")
        # Epoch mode
        parser.add_argument("--validator.epoch_minutes", type=int, default=None, help="Run in epoch mode with specified epoch length in minutes")
        parser.add_argument("--validator.continuous", action="store_true", help="Run in continuous epoch mode (default: enabled)")
        parser.add_argument("--no-validator.continuous", dest="validator_continuous", action="store_false", help="Disable continuous epoch mode")
        # Validator mode
        parser.add_argument("--validator.mode", choices=["bittensor", "mock"], default="bittensor", help="Validator mode: bittensor (production) or mock (simulation with real aggregator)")
        # Polling / backoff
        parser.add_argument("--validator.poll_seconds", type=int, default=12, help="Base polling interval in seconds")
        parser.add_argument("--validator.backoff_factor", type=float, default=2.0, help="Multiplicative error backoff factor")
        parser.add_argument("--validator.backoff_max_seconds", type=int, default=120, help="Maximum poll interval after backoff")
        parser.add_argument("--netuid", type=int, default=None, help="Target subnet UID for validator operations")
        
        config = bt.Config(parser)
        
        # Override from env
        netuid_env = os.getenv("NETUID")
        if netuid_env is not None:
            config.netuid = int(netuid_env)
        # Aggregator API overrides from env
        config.aggregator_url = os.getenv("AGGREGATOR_URL", getattr(config, "aggregator", None).url if hasattr(config, "aggregator") else "http://localhost:4100")
        # Validator API key (required for validator-specific endpoints)
        validator_api_key_env = os.getenv("VALIDATOR_API_KEY")
        if validator_api_key_env:
            config.validator_api_key = validator_api_key_env
        else:
            # Get from command line args
            config.validator_api_key = getattr(config, "validator", None).api_key if hasattr(config, "validator") else None
        aggregator_timeout_env = os.getenv("AGGREGATOR_TIMEOUT")
        if aggregator_timeout_env is not None:
            config.aggregator_timeout = int(aggregator_timeout_env)
        else:
            config.aggregator_timeout = getattr(config, "aggregator", None).timeout if hasattr(config, "aggregator") else 10

        aggregator_verify_ssl_env = os.getenv("AGGREGATOR_VERIFY_SSL")
        if aggregator_verify_ssl_env is not None:
            config.aggregator_verify_ssl = int(aggregator_verify_ssl_env)
        else:
            config.aggregator_verify_ssl = getattr(config, "aggregator", None).verify_ssl if hasattr(config, "aggregator") else 1

        aggregator_max_retries_env = os.getenv("AGGREGATOR_MAX_RETRIES")
        if aggregator_max_retries_env is not None:
            config.aggregator_max_retries = int(aggregator_max_retries_env)
        else:
            config.aggregator_max_retries = getattr(config, "aggregator", None).max_retries if hasattr(config, "aggregator") else 3
        try:
            config.aggregator_backoff_seconds = float(os.getenv("AGGREGATOR_BACKOFF_SECONDS", str(getattr(config, "aggregator", None).backoff_seconds if hasattr(config, "aggregator") else 0.5)))
        except Exception:
            config.aggregator_backoff_seconds = 0.5
        try:
            config.aggregator_page_limit = int(os.getenv("AGGREGATOR_PAGE_LIMIT", str(getattr(config, "aggregator", None).page_limit if hasattr(config, "aggregator") else 500)))
        except Exception:
            config.aggregator_page_limit = 500
        # Finalization buffer
        try:
            config.finalization_buffer_blocks = int(os.getenv(
                "VALIDATOR_FINALIZATION_BUFFER_BLOCKS",
                str(getattr(config, "finalization", None).buffer_blocks if hasattr(config, "finalization") else 6),
            ))
        except Exception:
            config.finalization_buffer_blocks = 6

        # Validation overrides
        try:
            config.validation_default_ttl_ms = int(os.getenv(
                "VALIDATION_DEFAULT_TTL_MS",
                str(getattr(config, "validation", None).default_ttl_ms if hasattr(config, "validation") else 1000),
            ))
        except Exception:
            config.validation_default_ttl_ms = 1000
        try:
            config.validation_max_response_latency_ms = int(os.getenv(
                "VALIDATION_MAX_RESPONSE_LATENCY_MS",
                str(getattr(config, "validation", None).max_response_latency_ms if hasattr(config, "validation") else 1500),
            ))
        except Exception:
            config.validation_max_response_latency_ms = 1500
        try:
            config.validation_max_clock_skew_seconds = int(os.getenv(
                "VALIDATION_MAX_CLOCK_SKEW_SECONDS",
                str(getattr(config, "validation", None).max_clock_skew_seconds if hasattr(config, "validation") else 1),
            ))
        except Exception:
            config.validation_max_clock_skew_seconds = 1

        # Burn allocation override
        config.burn_percentage = float(os.getenv(
            "BURN_PERCENTAGE",
            str(getattr(config, "burn_percentage", 0.0)),
        ))
        config.creator_miner_id = os.getenv(
            "CREATOR_MINER_ID",
            getattr(config, "creator_miner_id", None),
        )

        # Simulator overrides
        config.simulator_rpc_url = os.getenv(
            "SIMULATOR_RPC_URL",
            getattr(config, "simulator_rpc_url", None),
        )
        # Get simulator docker image - handle None/empty string cases
        env_image = os.getenv("SIMULATOR_DOCKER_IMAGE")
        config_image = getattr(config, "simulator_docker_image", None)
        config.simulator_docker_image = env_image or config_image or "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"
        
        # Ensure it's not None or empty
        if not config.simulator_docker_image:
            config.simulator_docker_image = "ghcr.io/subnet112/minotaur_contracts/mino-simulation:latest"
        
        # Get max concurrent simulations
        max_concurrent_default = getattr(config, "simulator_max_concurrent", None)
        if max_concurrent_default is None:
            max_concurrent_default = 5
        config.simulator_max_concurrent = int(os.getenv(
            "SIMULATOR_MAX_CONCURRENT",
            str(max_concurrent_default),
        ))

        # Validator ID override
        config.validator_id = os.getenv(
            "VALIDATOR_ID",
            getattr(config, "validator_id", None),
        )

        # Epoch mode overrides
        epoch_minutes_env = os.getenv("VALIDATOR_EPOCH_MINUTES")
        if epoch_minutes_env is not None:
            config.validator_epoch_minutes = int(epoch_minutes_env)
        else:
            config.validator_epoch_minutes = getattr(config, "validator_epoch_minutes", 5)
        # Default to True (continuous mode) unless explicitly disabled
        validator_continuous_env = os.getenv("VALIDATOR_CONTINUOUS")
        if validator_continuous_env is not None:
            # Environment variable takes precedence
            config.validator_continuous = validator_continuous_env.lower() in ("true", "1", "yes")
        elif "--no-validator.continuous" in sys.argv:
            # CLI flag --no-validator.continuous was explicitly used
            config.validator_continuous = False
        else:
            # Default to True if not set via env var or CLI
            config.validator_continuous = True

        # Validator mode overrides (CLI arg takes precedence, then env var, then default)
        validator_mode_cli = getattr(config, "validator", None) and getattr(config.validator, "mode", None)
        config.validator_mode = os.getenv("VALIDATOR_MODE", validator_mode_cli or "bittensor")


        # Polling/backoff overrides
        try:
            config.poll_seconds = int(os.getenv(
                "VALIDATOR_POLL_SECONDS",
                str(getattr(config, "validator", None).poll_seconds if hasattr(config, "validator") else 12),
            ))
        except Exception:
            config.poll_seconds = 12
        try:
            config.backoff_factor = float(os.getenv(
                "VALIDATOR_BACKOFF_FACTOR",
                str(getattr(config, "validator", None).backoff_factor if hasattr(config, "validator") else 2.0),
            ))
        except Exception:
            config.backoff_factor = 2.0
        try:
            config.backoff_max_seconds = int(os.getenv(
                "VALIDATOR_BACKOFF_MAX_SECONDS",
                str(getattr(config, "validator", None).backoff_max_seconds if hasattr(config, "validator") else 120),
            ))
        except Exception:
            config.backoff_max_seconds = 120
        
        return config
    
    def _validate_config(self) -> None:
        """Validate configuration parameters.
        
        Raises:
            ConfigurationException: If configuration is invalid
        """
        errors = []
        # Only validate netuid for Bittensor mode
        if self.config.validator_mode != "mock":
            if not isinstance(getattr(self.config, "netuid", None), int) or self.config.netuid is None or self.config.netuid < 0:
                errors.append("netuid must be a non-negative integer")
        if getattr(self.config, "finalization_buffer_blocks", 0) < 0:
            errors.append("finalization.buffer_blocks must be >= 0")
        if getattr(self.config, "poll_seconds", 0) <= 0:
            errors.append("validator.poll_seconds must be > 0")
        if getattr(self.config, "backoff_factor", 0) < 1.0:
            errors.append("validator.backoff_factor must be >= 1.0")
        if getattr(self.config, "backoff_max_seconds", 0) < getattr(self.config, "poll_seconds", 0):
            errors.append("validator.backoff_max_seconds must be >= validator.poll_seconds")
        for attr in (
            "validation_default_ttl_ms",
            "validation_max_response_latency_ms",
            "validation_max_clock_skew_seconds",
        ):
            value = getattr(self.config, attr, 0)
            if value is not None and value < 0:
                errors.append(f"{attr} must be non-negative")

        if errors:
            raise ConfigurationException("Validator configuration invalid: " + "; ".join(errors))
    
    def run(self):
        """Run the validator with asyncio loop"""
        try:
            validator_mode = getattr(self.config, "validator_mode", "bittensor")
            if validator_mode == "mock":
                self._run_mock_validator()
            else:
                self._run_bittensor_validator()
        except KeyboardInterrupt:
            self._log("Shutting down validator", LogLevel.INFO, prefix="RUN")
            sys.exit(0)

    def _run_mock_validator(self):
        """Run simulation validator (real aggregator, real simulation, no Bittensor)."""
        self._log("Starting simulation validator", LogLevel.INFO, prefix="RUN")
        self._log("⚠️  SIMULATION MODE: Real aggregator + real simulation, but NO Bittensor operations", LogLevel.WARNING, prefix="RUN")

        # Use the already initialized mock validator
        mock_validator = self._mock_validator

        async def main_async():
            if self.config.validator_continuous or self.config.validator_epoch_minutes:
                # Run continuous epochs
                epoch_minutes = self.config.validator_epoch_minutes or 1  # Shorter for testing
                await mock_validator.run_continuous_epochs(epoch_minutes)
            else:
                # Run test epochs
                await mock_validator.run_test_epochs(num_epochs=3, epoch_minutes=1)

        asyncio.run(main_async())

    def _run_bittensor_validator(self):
        """Run Bittensor validator with real blockchain operations."""
        self._log("Starting Bittensor validator", LogLevel.INFO, prefix="RUN")

        # Create Bittensor validator
        bittensor_validator = BittensorValidator(
            config=self.config,
            subtensor=self.subtensor,
            wallet=self.wallet,
            logger=bt.logging
        )

        async def main_async():
            # Initialize Bittensor components
            await bittensor_validator.start()

            if self.config.validator_continuous or self.config.validator_epoch_minutes:
                # Run continuous epochs
                epoch_minutes = self.config.validator_epoch_minutes or 5
                await bittensor_validator.run_continuous_epochs(epoch_minutes)
            else:
                # Bittensor mode but not using epochs - this shouldn't happen with current design
                self._log("Error: Bittensor mode requires epoch configuration", LogLevel.ERROR, prefix="CONFIG")
                raise ValueError("Bittensor mode requires --validator.continuous or --validator.epoch_minutes")

            # Cleanup
            await bittensor_validator.stop()

        asyncio.run(main_async())



def main():
    """Entry point for the validator."""
    validator = Validator()
    validator.run()


if __name__ == "__main__":
    main()
