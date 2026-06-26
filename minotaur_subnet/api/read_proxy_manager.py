"""API-launched block-pin RPC proxy — the api owns the proxy as a managed container.

The block-pin proxy (the solver's deterministic read path) MUST run as a separate
container on the sealed ``benchmark-sandbox`` network: the untrusted solver reaches
it there, and the api can't join that net itself without exposing the api to the
solver. The naive way to ship it is a compose service — but a Watchtower-only
validator never gets a NEW compose service without pulling the new compose. So
instead the API LAUNCHES it at startup, from the api's OWN image, via the same
docker-socket-proxy it already uses to spawn solver benchmark containers. The whole
determinism rollout then rides the normal ``:stable`` image update with ZERO
operator action: Watchtower updates the api image, the api re-creates the proxy
from it.

Idempotent: a running proxy on the current image is left alone; a stale or missing
one is (re)created. On success the api exports the ``SOLVER_READ_PROXY*`` env so the
existing :func:`minotaur_subnet.harness.solver_read_proxy.read_proxy_config` wiring
picks it up unchanged. On failure it logs loudly and exports nothing — the benchmark
then fails loud (no proxy = no deterministic read path), which drops the node from
the adoption quorum rather than silently mis-scoring.

Runs on BOTH leaders (proactive benchmark worker) and followers (reactive champion
verification) — both score through the same ``run_benchmark`` read path.

Disable with ``DISABLE_READ_PROXY=1`` (dev / local without docker-socket access).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)

PROXY_CONTAINER_NAME = "minotaur-rpc-pin-proxy"
PROXY_STATIC_IP = "172.30.0.5"
PROXY_PORT = "8645"
_PROXY_MODULE = "minotaur_subnet.harness.rpc_budget_proxy.proxy"

# Data plane the SOLVER dials (sandbox net, static IP) and control plane the API
# dials (the managed container's name on the validator/minotaur net).
PROXY_DATA_URL = f"http://{PROXY_STATIC_IP}:{PROXY_PORT}"
PROXY_CONTROL_URL = f"http://{PROXY_CONTAINER_NAME}:{PROXY_PORT}"

_FALSEY = {"0", "false", "no", "off", ""}


def read_proxy_launch_disabled() -> bool:
    """True iff ``DISABLE_READ_PROXY`` is set to a truthy value (default: launch)."""
    return os.environ.get("DISABLE_READ_PROXY", "").strip().lower() not in _FALSEY


async def _docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a docker CLI command (via the api's ``DOCKER_HOST``); return (rc, out, err)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace").strip(),
            err.decode("utf-8", "replace").strip(),
        )
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        return 1, "", str(exc)


def _build_upstreams() -> str:
    """``UPSTREAMS`` from the api's live read RPCs (the same upstreams the anvils fork)."""
    eth = os.environ.get("ETH_RPC_URL") or os.environ.get("ETH_UPSTREAM_RPC_URL") or ""
    base = os.environ.get("BASE_RPC_URL") or os.environ.get("BASE_UPSTREAM_RPC_URL") or ""
    btevm = (
        os.environ.get("BITTENSOR_EVM_RPC_URL")
        or os.environ.get("BITTENSOR_EVM_UPSTREAM_RPC_URL")
        or "https://lite.chain.opentensor.ai"
    )
    return ",".join(
        f"{k}={v}" for k, v in (("eth", eth), ("base", base), ("btevm", btevm)) if v
    )


def _export_env(token: str) -> None:
    """Point the existing ``read_proxy_config`` wiring at the launched proxy.

    ``setdefault`` so an operator who pinned an explicit URL (e.g. a custom lead
    topology) always wins; a bare validator gets the managed defaults.
    """
    os.environ.setdefault("SOLVER_READ_PROXY", PROXY_DATA_URL)
    os.environ.setdefault("SOLVER_READ_PROXY_CONTROL", PROXY_CONTROL_URL)
    if token:
        os.environ.setdefault("SOLVER_READ_PROXY_TOKEN", token)


async def ensure_read_proxy_container() -> bool:
    """Ensure the block-pin proxy runs as a managed container on the api's image.

    Returns True if the proxy is up + the env exported, else False (logged). Safe and
    idempotent to call once at api startup.
    """
    if read_proxy_launch_disabled():
        logger.info("[read-proxy] DISABLE_READ_PROXY set — not launching the managed proxy")
        return False

    # Self-inspect: the api's OWN image (so the proxy is the api's exact image and
    # auto-updates with it) + the api's networks (to find the minotaur/validator net).
    rc, info, err = await _docker(
        "inspect", socket.gethostname(),
        "--format", "{{.Image}}|{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
    )
    if rc != 0 or "|" not in info:
        logger.error(
            "[read-proxy] cannot inspect self (no docker-socket access?): %s — proxy NOT "
            "launched; benchmarks will fail loud rather than mis-score", err or info,
        )
        return False
    image, nets_s = info.split("|", 1)
    image = image.strip()
    sandbox = os.environ.get("BENCHMARK_DOCKER_NETWORK", "benchmark-sandbox").strip()
    minotaur_net = next((n for n in nets_s.split() if n and n != sandbox), None)
    token = os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
    upstreams = _build_upstreams()

    # Idempotency: a running proxy already on the current image is correct as-is.
    rc, cur, _ = await _docker(
        "inspect", PROXY_CONTAINER_NAME, "--format", "{{.State.Running}}|{{.Image}}",
    )
    if rc == 0:
        parts = (cur.split("|", 1) + [""])[:2]
        if parts[0].strip() == "true" and parts[1].strip() == image:
            logger.info("[read-proxy] managed proxy already running on the current image")
            _export_env(token)
            return True
        await _docker("rm", "-f", PROXY_CONTAINER_NAME)  # stale image / stopped -> replace

    # NOTE: no Watchtower label — the api (not Watchtower) owns this container's
    # lifecycle; it is re-created from the new image when the api itself updates.
    create = [
        "run", "-d", "--name", PROXY_CONTAINER_NAME, "--restart", "unless-stopped",
        "--network", sandbox, "--ip", PROXY_STATIC_IP,
        "-e", f"UPSTREAMS={upstreams}",
        "-e", f"CONTROL_TOKEN={token}",
        "-e", f"LISTEN_PORT={PROXY_PORT}",
        "-e", "LOG_LEVEL=INFO",
        image, "-m", _PROXY_MODULE,
    ]
    rc, _cid, err = await _docker(*create, timeout=90)
    if rc != 0:
        logger.error(
            "[read-proxy] FAILED to launch managed proxy: %s — benchmarks will fail loud "
            "(no deterministic read path); node drops from quorum, never mis-scores", err,
        )
        return False

    # Attach the minotaur/validator net too (egress to upstream + the api's control plane).
    if minotaur_net:
        rcn, _, errn = await _docker("network", "connect", minotaur_net, PROXY_CONTAINER_NAME)
        if rcn != 0:
            logger.warning(
                "[read-proxy] could not attach %s to %s: %s",
                PROXY_CONTAINER_NAME, minotaur_net, errn,
            )

    _export_env(token)
    logger.info(
        "[read-proxy] launched managed proxy %s on %s(.5)+%s from image %s",
        PROXY_CONTAINER_NAME, sandbox, minotaur_net, image[:24],
    )
    return True
