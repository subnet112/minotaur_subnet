#!/usr/bin/env python3
"""
Minotaur Validator auto-update runner.

This script runs a validator process and automatically updates it when a new version is released.
Command-line arguments will be forwarded to validator (`neurons/validator.py`), so you can pass
them like this:
    python3 scripts/start_validator.py --wallet.name=my-wallet --netuid=XX

Auto-updates are enabled by default and will make sure that the latest version is always running
by pulling the latest version from git and upgrading python packages. This is done periodically.
Local changes may prevent the update, but they will be preserved.

To disable auto-updates, pass --no_autoupdate.

The script will use the same virtual environment as the one used to run it. If you want to run
validator within virtual environment, run this auto-update script from the virtual environment.

PM2 is required for this script. This script will start a pm2 process using the name provided by
the --pm2_name argument.

Usage:
    python3 scripts/start_validator.py --pm2_name minotaur_vali --wallet.name my_wallet --netuid 64
    python3 scripts/start_validator.py --no_autoupdate --pm2_name minotaur_vali
"""
import argparse
import logging
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from shlex import split
from typing import List, Optional

log = logging.getLogger(__name__)

# How often to check for updates (default: 15 minutes)
UPDATES_CHECK_TIME = timedelta(minutes=15)

# Root directory of the repository
ROOT_DIR = Path(__file__).parent.parent


def get_version() -> str:
    """Extract the version as current git commit hash."""
    result = subprocess.run(
        split("git rev-parse HEAD"),
        check=True,
        capture_output=True,
        cwd=ROOT_DIR,
    )
    commit = result.stdout.decode().strip()
    assert len(commit) == 40, f"Invalid commit hash: {commit}"
    return commit[:8]


def get_pm2_process_status(pm2_name: str) -> Optional[str]:
    """Get the status of a PM2 process by name."""
    try:
        result = subprocess.run(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
        )
        if result.returncode != 0:
            return None

        import json
        processes = json.loads(result.stdout)
        for proc in processes:
            if proc.get("name") == pm2_name:
                return proc.get("pm2_env", {}).get("status", "unknown")
        return None
    except Exception:
        return None


def start_validator_process(pm2_name: str, args: List[str]) -> subprocess.Popen:
    """
    Spawn a new python process running neurons.validator via PM2.

    `sys.executable` ensures that the same python interpreter is used as the one
    used to run this auto-updater.
    """
    assert sys.executable, "Failed to get python executable"

    log.info("Starting validator process with pm2, name: %s", pm2_name)

    # Check if process already exists and delete it first
    existing_status = get_pm2_process_status(pm2_name)
    if existing_status is not None:
        log.info("Removing existing PM2 process: %s (status: %s)", pm2_name, existing_status)
        subprocess.run(["pm2", "delete", pm2_name], cwd=ROOT_DIR, capture_output=True)

    process = subprocess.Popen(
        [
            "pm2",
            "start",
            sys.executable,
            "--name",
            pm2_name,
            "--",
            "-m",
            "neurons.validator",
            *args,
        ],
        cwd=ROOT_DIR,
    )
    process.pm2_name = pm2_name

    # Wait for PM2 to start the process
    time.sleep(2)

    status = get_pm2_process_status(pm2_name)
    if status == "online":
        log.info("Validator started successfully (PM2 status: %s)", status)
    else:
        log.warning("Validator may not have started correctly (PM2 status: %s)", status)

    return process


def stop_validator_process(process: subprocess.Popen) -> None:
    """Stop the validator process via PM2."""
    log.info("Stopping validator process: %s", process.pm2_name)
    subprocess.run(["pm2", "delete", process.pm2_name], cwd=ROOT_DIR, check=True)


def restart_validator_process(pm2_name: str) -> None:
    """Restart the validator process via PM2."""
    log.info("Restarting validator process: %s", pm2_name)
    subprocess.run(["pm2", "restart", pm2_name], cwd=ROOT_DIR, check=True)


def pull_latest_version() -> bool:
    """
    Pull the latest version from git.

    This uses `git pull --rebase`, so if any changes were made to the local repository,
    this will try to apply them on top of origin's changes. This is intentional, as we
    don't want to overwrite any local changes. However, if there are any conflicts,
    this will abort the rebase and return to the original state.

    The conflicts are expected to happen rarely since validator is expected
    to be used as-is.

    Returns:
        True if pull was successful, False if there were conflicts
    """
    try:
        result = subprocess.run(
            split("git pull --rebase --autostash"),
            check=True,
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
        )
        log.debug("Git pull output: %s", result.stdout)
        return True
    except subprocess.CalledProcessError as exc:
        log.error("Failed to pull, reverting: %s", exc)
        subprocess.run(split("git rebase --abort"), cwd=ROOT_DIR, capture_output=True)
        return False


def upgrade_packages() -> None:
    """
    Upgrade python packages by running `pip install -e .` or `pip install -r requirements.txt`.

    This ensures that any new dependencies are installed after an update.
    """
    log.info("Upgrading packages...")

    # First try pip install -e . if setup.py or pyproject.toml exists
    setup_py = ROOT_DIR / "setup.py"
    pyproject = ROOT_DIR / "pyproject.toml"

    if setup_py.exists() or pyproject.exists():
        try:
            subprocess.run(
                split(f"{sys.executable} -m pip install -e ."),
                check=True,
                cwd=ROOT_DIR,
                capture_output=True,
            )
            log.info("Packages upgraded via pip install -e .")
            return
        except subprocess.CalledProcessError as exc:
            log.warning("pip install -e . failed, trying requirements.txt: %s", exc)

    # Fallback to requirements.txt
    requirements = ROOT_DIR / "requirements.txt"
    if requirements.exists():
        try:
            subprocess.run(
                split(f"{sys.executable} -m pip install -r requirements.txt"),
                check=True,
                cwd=ROOT_DIR,
                capture_output=True,
            )
            log.info("Packages upgraded via requirements.txt")
        except subprocess.CalledProcessError as exc:
            log.error("Failed to upgrade packages, proceeding anyway: %s", exc)
    else:
        log.warning("No requirements.txt or setup.py found, skipping package upgrade")


def check_pm2_installed() -> bool:
    """Check if PM2 is installed and available."""
    try:
        result = subprocess.run(
            ["pm2", "--version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("PM2 version: %s", result.stdout.strip())
            return True
        return False
    except FileNotFoundError:
        return False


def main_with_autoupdate(pm2_name: str, args: List[str]) -> None:
    """
    Run the validator process and automatically update it when a new version is released.

    This will check for updates every `UPDATES_CHECK_TIME` and update the validator
    if a new version is available. Update is performed as simple `git pull --rebase`.
    """
    validator = start_validator_process(pm2_name, args)
    current_version = get_version()
    log.info("Current version: %s", current_version)

    try:
        while True:
            time.sleep(UPDATES_CHECK_TIME.total_seconds())

            log.info("Checking for updates...")
            if not pull_latest_version():
                log.warning("Failed to pull latest version, will retry later")
                continue

            latest_version = get_version()
            log.info("Latest version: %s", latest_version)

            if latest_version != current_version:
                log.info(
                    "Upgraded to latest version: %s -> %s",
                    current_version,
                    latest_version,
                )
                upgrade_packages()

                # Restart the validator with PM2
                stop_validator_process(validator)
                validator = start_validator_process(pm2_name, args)
                current_version = latest_version
            else:
                log.info("Already at latest version")

    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        stop_validator_process(validator)


def main_without_autoupdate(pm2_name: str, args: List[str]) -> None:
    """
    Run the validator process without auto-updates.

    The script will just start the validator and wait for it to exit or for user interrupt.
    """
    validator = start_validator_process(pm2_name, args)
    current_version = get_version()
    log.info("Current version: %s (auto-update disabled)", current_version)

    try:
        # Just wait indefinitely - PM2 manages the process
        while True:
            time.sleep(60)
            status = get_pm2_process_status(pm2_name)
            if status != "online":
                log.warning("Validator process status: %s", status)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        stop_validator_process(validator)


def main():
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Automatically update and restart the Minotaur validator process when a new version is released.",
        epilog="""
Example usage:
    python scripts/start_validator.py --pm2_name minotaur_vali --wallet.name wallet1 --netuid 64
    python scripts/start_validator.py --no_autoupdate --pm2_name minotaur_vali
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--pm2_name",
        default="minotaur_vali",
        help="Name of the PM2 process (default: minotaur_vali)",
    )
    parser.add_argument(
        "--no_autoupdate",
        action="store_true",
        help="Disable automatic updates",
    )
    parser.add_argument(
        "--update_interval",
        type=int,
        default=15,
        help="Update check interval in minutes (default: 15)",
    )

    flags, extra_args = parser.parse_known_args()

    # Check PM2 is installed
    if not check_pm2_installed():
        log.error("PM2 is not installed. Please install it with: npm install -g pm2")
        sys.exit(1)

    # Set update interval
    global UPDATES_CHECK_TIME
    UPDATES_CHECK_TIME = timedelta(minutes=flags.update_interval)
    log.info("Update check interval: %s minutes", flags.update_interval)

    if flags.no_autoupdate:
        log.info("Auto-updates disabled")
        main_without_autoupdate(flags.pm2_name, extra_args)
    else:
        log.info("Auto-updates enabled")
        main_with_autoupdate(flags.pm2_name, extra_args)


if __name__ == "__main__":
    main()
