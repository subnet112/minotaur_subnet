"""The untrusted-miner ``docker build`` carries host-resource caps.

Screening Stage-2 builds an untrusted, miner-submitted Dockerfile on the
validator's SHARED host docker daemon (no rootless/isolated builder — issue
#472). ``_solver_build_command`` bounds what a malicious Dockerfile's ``RUN``
steps can consume: network-off, RSS + swap cap, CPU quota, and an fd ulimit —
all legacy-builder-compatible (BuildKit can't run behind the docker-socket-proxy).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from minotaur_subnet.harness.screening import _solver_build_command


def _flag_value(cmd: list[str], flag: str) -> str | None:
    """Value of ``--flag=value`` (or the token after a bare ``--flag``)."""
    for i, tok in enumerate(cmd):
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
        if tok == flag and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def test_default_caps_present(monkeypatch):
    for var in ("SCREENING_BUILD_MEMORY", "SCREENING_BUILD_CPU_PERIOD",
                "SCREENING_BUILD_CPU_QUOTA", "SCREENING_BUILD_NOFILE"):
        monkeypatch.delenv(var, raising=False)

    cmd = _solver_build_command("img:tag", "/repo")

    assert cmd[:2] == ["docker", "build"]
    assert "--network=none" in cmd
    assert "--no-cache" in cmd
    assert _flag_value(cmd, "--memory") == "4g"
    # swap cap equals the memory cap → swap escape disabled.
    assert _flag_value(cmd, "--memory-swap") == "4g"
    assert _flag_value(cmd, "--cpu-period") == "100000"
    assert _flag_value(cmd, "--cpu-quota") == "200000"  # 2 CPUs
    assert _flag_value(cmd, "--ulimit") == "nofile=4096:4096"
    # tag + build context come last.
    assert _flag_value(cmd, "-t") == "img:tag"
    assert cmd[-1] == "/repo"


def test_memory_swap_tracks_memory_override(monkeypatch):
    """--memory-swap must follow --memory so swap stays disabled when the cap
    is retuned (otherwise a 2x-memory swap escape silently reopens)."""
    monkeypatch.setenv("SCREENING_BUILD_MEMORY", "2g")
    cmd = _solver_build_command("img:tag", "/repo")
    assert _flag_value(cmd, "--memory") == "2g"
    assert _flag_value(cmd, "--memory-swap") == "2g"


def test_env_overrides_are_honored(monkeypatch):
    monkeypatch.setenv("SCREENING_BUILD_MEMORY", "8g")
    monkeypatch.setenv("SCREENING_BUILD_CPU_PERIOD", "50000")
    monkeypatch.setenv("SCREENING_BUILD_CPU_QUOTA", "50000")  # 1 CPU
    monkeypatch.setenv("SCREENING_BUILD_NOFILE", "1024")

    cmd = _solver_build_command("img:tag", "/repo")

    assert _flag_value(cmd, "--memory") == "8g"
    assert _flag_value(cmd, "--memory-swap") == "8g"
    assert _flag_value(cmd, "--cpu-period") == "50000"
    assert _flag_value(cmd, "--cpu-quota") == "50000"
    assert _flag_value(cmd, "--ulimit") == "nofile=1024:1024"


def test_blank_env_falls_back_to_default(monkeypatch):
    """An empty/whitespace env value must not produce a malformed flag."""
    monkeypatch.setenv("SCREENING_BUILD_MEMORY", "   ")
    cmd = _solver_build_command("img:tag", "/repo")
    assert _flag_value(cmd, "--memory") == "4g"
    assert _flag_value(cmd, "--memory-swap") == "4g"


def test_uses_legacy_compatible_flags_only(monkeypatch):
    """Guard against a future edit adding a build flag the legacy builder (behind
    the socket-proxy) rejects — e.g. --pids-limit is a `docker run` flag, not a
    `docker build` one."""
    cmd = _solver_build_command("img:tag", "/repo")
    assert "--pids-limit" not in cmd
    assert not any(t.startswith("--pids-limit") for t in cmd)
