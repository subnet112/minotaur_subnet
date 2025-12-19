"""Version information for Minotaur Validator.

This module provides version information that should match GitHub releases.
Update this file when creating new releases.
"""

import os

__version__ = "0.1.0"
__version_info__ = tuple(map(int, __version__.split(".")))

# GitHub repository information
# Set via environment variable GITHUB_REPO or configure after cloning
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest" if GITHUB_REPO else None

def get_version():
    """Get the current version string."""
    return __version__

def get_version_info():
    """Get version as tuple (major, minor, patch)."""
    return __version_info__

def is_dev_version():
    """Check if this is a development version (not a release)."""
    return "dev" in __version__.lower() or "rc" in __version__.lower()

def __str__():
    return f"Minotaur Validator v{__version__}"

def __repr__():
    return f"Version({__version__})"
