"""A solver must not be able to fill the validator's disk with log output.

The sandbox bounds every other resource a solver can consume — memory, swap,
CPU, pids, tmpfs, read-only rootfs — but stdout went to docker's default
json-file driver, which has NO size limit. On 2026-08-22 three solver
containers wrote 74.5GB + 60.1GB + 11.1GB in ~13h, filled the leader's 193GB
root and took the api down: /health still answered 200 while every
store-backed endpoint 500'd and `docker exec` failed with "no space left on
device". Rounds stalled ~5h.
"""
from minotaur_subnet.harness.orchestrator import DOCKER_SECURITY_OPTS


def _opt_value(name):
    """Value of a `--log-opt k=v` pair, which docker takes as two argv items."""
    for i, tok in enumerate(DOCKER_SECURITY_OPTS):
        if tok == "--log-opt" and i + 1 < len(DOCKER_SECURITY_OPTS):
            k, _, v = DOCKER_SECURITY_OPTS[i + 1].partition("=")
            if k == name:
                return v
    return None


def test_solver_logs_are_size_capped():
    assert _opt_value("max-size") is not None, "solver stdout is uncapped"


def test_solver_logs_are_file_capped():
    """max-size alone only bounds ONE file; without max-file docker keeps
    rotating and the total is still unbounded."""
    assert _opt_value("max-file") is not None


def test_total_log_budget_is_bounded_and_modest():
    size = _opt_value("max-size")
    files = int(_opt_value("max-file"))
    assert size.endswith("m"), f"expect megabytes, got {size!r}"
    total_mb = int(size[:-1]) * files
    assert 0 < total_mb <= 256, f"per-solver log budget {total_mb}MB is too large"


def test_log_driver_is_pinned_to_json_file():
    """`max-size`/`max-file` are driver-specific: a host defaulting to journald
    rejects them outright and EVERY benchmark run would fail to start. Pinning
    the driver keeps the cap self-consistent regardless of the daemon default."""
    assert "--log-driver=json-file" in DOCKER_SECURITY_OPTS


def test_the_other_sandbox_caps_are_still_present():
    """Regression guard: the log cap must not have displaced anything."""
    for opt in ("--network=none", "--read-only", "--cap-drop=ALL",
                "--memory=4g", "--cpus=2.0", "--pids-limit=256"):
        assert opt in DOCKER_SECURITY_OPTS, opt
