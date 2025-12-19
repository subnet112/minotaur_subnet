#!/usr/bin/env python3
"""Automatic validator upgrader.

Checks for new releases on GitHub and upgrades the validator if available.
This script should be run periodically to keep the validator up-to-date.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any
import urllib.request
import urllib.error

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from neurons.version import get_version, get_version_info, GITHUB_API_URL, GITHUB_REPO


class ValidatorUpgrader:
    """Handles automatic upgrades of the Minotaur validator."""

    def __init__(self, log_level: str = "INFO"):
        self.logger = self._setup_logger(log_level)
        self.current_version = get_version()
        self.current_version_info = get_version_info()

        # Get the validator directory
        self.validator_dir = Path(__file__).parent.parent
        self.temp_dir = Path(tempfile.gettempdir()) / "minotaur_upgrade"

        self.logger.info(f"Validator Upgrader initialized for v{self.current_version}")
        self.logger.info(f"Validator directory: {self.validator_dir}")

    def _setup_logger(self, log_level: str) -> logging.Logger:
        """Setup logging for the upgrader."""
        logger = logging.getLogger("upgrader")
        logger.setLevel(getattr(logging, log_level.upper()))

        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def check_for_updates(self) -> Optional[Dict[str, Any]]:
        """Check GitHub for the latest release."""
        self.logger.info("Checking for validator updates...")

        # Check if GitHub repo is configured
        if not GITHUB_REPO or not GITHUB_API_URL:
            self.logger.warning("GITHUB_REPO not configured. Set GITHUB_REPO environment variable to enable automatic updates.")
            return None

        try:
            # Query GitHub API for latest release
            req = urllib.request.Request(GITHUB_API_URL)
            req.add_header('User-Agent', f'Minotaur-Validator-Upgrader/{self.current_version}')

            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status != 200:
                    self.logger.error(f"GitHub API returned status {response.status}")
                    return None

                data = json.loads(response.read().decode('utf-8'))

            latest_version = data.get('tag_name', '').lstrip('v')
            if not latest_version:
                self.logger.error("Could not parse latest version from GitHub response")
                return None

            # Parse version
            try:
                latest_version_info = tuple(map(int, latest_version.split('.')))
            except ValueError:
                self.logger.error(f"Invalid version format: {latest_version}")
                return None

            # Compare versions
            if latest_version_info > self.current_version_info:
                self.logger.info(f"New version available: v{latest_version} (current: v{self.current_version})")
                return {
                    'version': latest_version,
                    'version_info': latest_version_info,
                    'release_data': data
                }
            elif latest_version_info < self.current_version_info:
                self.logger.info(f"Current version v{self.current_version} is newer than latest release v{latest_version}")
                return None
            else:
                self.logger.info(f"Validator is up-to-date (v{self.current_version})")
                return None

        except urllib.error.URLError as e:
            self.logger.error(f"Network error checking for updates: {e}")
            return None
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing GitHub API response: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error checking for updates: {e}")
            return None

    def download_release(self, release_data: Dict[str, Any]) -> Optional[Path]:
        """Download the latest release asset."""
        self.logger.info("Downloading latest release...")

        try:
            # Find the source code asset (tar.gz)
            assets = release_data.get('assets', [])
            source_asset = None

            for asset in assets:
                name = asset.get('name', '')
                if name.endswith('.tar.gz') and 'source' in name.lower():
                    source_asset = asset
                    break

            if not source_asset:
                # Fallback: look for any tar.gz asset
                for asset in assets:
                    if asset.get('name', '').endswith('.tar.gz'):
                        source_asset = asset
                        break

            if not source_asset:
                # If no assets, try to download from the tarball_url
                tarball_url = release_data.get('tarball_url')
                if tarball_url:
                    download_url = tarball_url
                    filename = f"minotaur-{release_data['tag_name']}.tar.gz"
                else:
                    self.logger.error("No downloadable assets found in release")
                    return None
            else:
                download_url = source_asset['browser_download_url']
                filename = source_asset['name']

            # Create temp directory
            self.temp_dir.mkdir(exist_ok=True)
            download_path = self.temp_dir / filename

            self.logger.info(f"Downloading from: {download_url}")
            self.logger.info(f"Saving to: {download_path}")

            # Download the file
            req = urllib.request.Request(download_url)
            req.add_header('User-Agent', f'Minotaur-Validator-Upgrader/{self.current_version}')

            with urllib.request.urlopen(req, timeout=300) as response:
                if response.status != 200:
                    self.logger.error(f"Download failed with status {response.status}")
                    return None

                with open(download_path, 'wb') as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

            self.logger.info(f"Download completed: {download_path}")
            return download_path

        except Exception as e:
            self.logger.error(f"Error downloading release: {e}")
            return None

    def extract_and_install(self, archive_path: Path, release_data: Dict[str, Any]) -> bool:
        """Extract the downloaded archive and install the new version."""
        self.logger.info("Extracting and installing new version...")

        try:
            import tarfile

            # Extract archive
            extract_dir = self.temp_dir / "extracted"
            extract_dir.mkdir(exist_ok=True)

            self.logger.info(f"Extracting {archive_path} to {extract_dir}")

            with tarfile.open(archive_path, 'r:gz') as tar:
                # Get the top-level directory name
                members = tar.getmembers()
                if not members:
                    raise ValueError("Empty archive")

                # Find the top-level directory
                top_level_dirs = [m for m in members if m.isdir() and '/' not in m.name]
                if top_level_dirs:
                    top_level_dir = top_level_dirs[0].name
                else:
                    # If no top-level dir, extract to current dir name
                    top_level_dir = f"minotaur-{release_data['tag_name'].lstrip('v')}"

                # Extract all files
                tar.extractall(extract_dir, members=members)

            extracted_path = extract_dir / top_level_dir
            if not extracted_path.exists():
                # Try without top-level directory
                extracted_path = extract_dir

            self.logger.info(f"Extracted to: {extracted_path}")

            # Backup current version
            backup_dir = self.validator_dir.parent / f"minotaur-backup-{self.current_version}"
            if backup_dir.exists():
                import shutil
                shutil.rmtree(backup_dir)

            self.logger.info(f"Backing up current version to: {backup_dir}")
            shutil.copytree(self.validator_dir, backup_dir, symlinks=True)

            # Install new version (copy over existing files)
            self.logger.info("Installing new version...")
            self._copy_tree(extracted_path, self.validator_dir)

            self.logger.info("Installation completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Error during extraction/installation: {e}")
            return False

    def _copy_tree(self, src: Path, dst: Path):
        """Copy directory tree, preserving symlinks."""
        import shutil

        for item in src.iterdir():
            if item.name.startswith('.'):  # Skip hidden files
                continue

            dst_item = dst / item.name

            if item.is_file():
                shutil.copy2(item, dst_item)
            elif item.is_dir():
                if dst_item.exists():
                    shutil.rmtree(dst_item)
                shutil.copytree(item, dst_item, symlinks=True)

    def find_validator_process(self) -> Optional[int]:
        """Find the PID of the running validator process."""
        try:
            # Use pgrep to find validator processes
            result = subprocess.run(
                ['pgrep', '-f', 'neurons.validator'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                # Get the first PID
                pids = result.stdout.strip().split('\n')
                if pids and pids[0]:
                    return int(pids[0])

        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass

        return None

    def restart_validator(self) -> bool:
        """Restart the validator process."""
        self.logger.info("Restarting validator...")

        try:
            # Find and stop current validator
            pid = self.find_validator_process()
            if pid:
                self.logger.info(f"Stopping validator process (PID: {pid})")
                os.kill(pid, signal.SIGTERM)

                # Wait for it to stop
                for _ in range(30):  # Wait up to 30 seconds
                    time.sleep(1)
                    if not self.find_validator_process():
                        break

                # Force kill if still running
                if self.find_validator_process():
                    self.logger.warning("Force killing validator process")
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(2)

            # Start new validator
            self.logger.info("Starting new validator version")

            # Change to validator directory
            os.chdir(self.validator_dir)

            # Start validator (assuming it's running in background)
            # This is a simple restart - you may need to customize based on your setup
            env = os.environ.copy()
            env['PYTHONPATH'] = str(self.validator_dir)

            # Try to determine how the validator was started
            cmd = [sys.executable, '-m', 'neurons.validator']

            # Add any command line arguments from current process if available
            # For now, just start with basic config
            cmd.extend(['--validator.mode', 'bittensor'])  # Default to bittensor mode

            self.logger.info(f"Starting validator with command: {' '.join(cmd)}")

            # Start in background
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid  # Create new process group
            )

            # Wait a bit and check if it's still running
            time.sleep(5)
            if process.poll() is None:
                self.logger.info(f"Validator restarted successfully (PID: {process.pid})")
                return True
            else:
                stdout, stderr = process.communicate()
                self.logger.error(f"Validator failed to start: {stderr.decode()}")
                return False

        except Exception as e:
            self.logger.error(f"Error restarting validator: {e}")
            return False

    async def upgrade(self, force: bool = False, skip_restart: bool = False) -> bool:
        """Perform the complete upgrade process."""
        self.logger.info("Starting validator upgrade process...")

        # Check if GitHub repo is configured
        if not GITHUB_REPO:
            self.logger.error("GITHUB_REPO not configured. Cannot perform upgrade. Set GITHUB_REPO environment variable.")
            return False

        # Check for updates
        update_info = self.check_for_updates()
        if not update_info and not force:
            self.logger.info("No updates available")
            return True

        if not update_info and force:
            self.logger.info("Forcing upgrade (no update info available)")
            update_info = {'version': 'latest', 'release_data': {}}

        # Download release
        archive_path = self.download_release(update_info['release_data'])
        if not archive_path:
            self.logger.error("Failed to download release")
            return False

        # Extract and install
        if not self.extract_and_install(archive_path, update_info['release_data']):
            self.logger.error("Failed to install new version")
            return False

        # Clean up temp files
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception as e:
            self.logger.warning(f"Failed to clean up temp files: {e}")

        self.logger.info(f"Successfully upgraded to version {update_info['version']}")

        # Restart validator
        if not skip_restart:
            if not self.restart_validator():
                self.logger.error("Failed to restart validator")
                return False

        self.logger.info("Upgrade completed successfully!")
        return True


def main():
    """Main entry point for the upgrader script."""
    parser = argparse.ArgumentParser(description="Minotaur Validator Upgrader")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check for updates, don't install"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force upgrade even if no newer version is available"
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="Skip restarting the validator after upgrade"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set logging level"
    )

    args = parser.parse_args()

    # Create upgrader
    upgrader = ValidatorUpgrader(log_level=args.log_level)

    if args.check_only:
        # Just check for updates
        update_info = upgrader.check_for_updates()
        if update_info:
            print(f"New version available: v{update_info['version']}")
            return 0
        else:
            print("No updates available")
            return 0

    # Perform upgrade
    try:
        success = asyncio.run(upgrader.upgrade(
            force=args.force,
            skip_restart=args.skip_restart
        ))
        return 0 if success else 1

    except KeyboardInterrupt:
        print("Upgrade interrupted by user")
        return 1
    except Exception as e:
        print(f"Upgrade failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
