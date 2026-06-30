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


async def _proxy_state() -> tuple[bool, str]:
    """``(running?, image)`` of the managed proxy.

    Tries ``docker inspect`` (precise image — so the proxy can be RECREATED when the api
    image changes, e.g. a consensus-relevant rewrite-table version bump; needs the docker
    CLI's API version pinned via ``DOCKER_API_VERSION``, else the unpinned CLI's version
    negotiation gets a 403 from the socket-proxy). Falls back to ``docker ps``
    (running-only, image ``""``) where inspect is denied — ``GET /containers/json`` is
    allowed even where ``GET /containers/<id>/json`` 403s. Never blocks on the socket.
    """
    rc, out, _ = await _docker(
        "inspect", PROXY_CONTAINER_NAME, "--format", "{{.State.Running}}|{{.Image}}",
    )
    if rc == 0 and "|" in out:
        running, image = (out.split("|", 1) + [""])[:2]
        return running.strip() == "true", image.strip()
    rc, names, _ = await _docker(
        "ps", "--filter", f"name=^{PROXY_CONTAINER_NAME}$", "--format", "{{.Names}}",
    )
    return (rc == 0 and PROXY_CONTAINER_NAME in names.split()), ""


async def _resolve_self_image_and_net() -> tuple[str, str | None]:
    """The api's own image + its minotaur/validator network (to launch the proxy from the
    same image, attached to the same egress net).

    Tries the precise ``docker inspect self``; falls back to ``docker ps`` where the
    socket-proxy denies inspect-by-id (403). Returns ("", None) if both fail.
    """
    host = socket.gethostname()
    sandbox = os.environ.get("BENCHMARK_DOCKER_NETWORK", "benchmark-sandbox").strip()
    rc, info, _ = await _docker(
        "inspect", host,
        "--format", "{{.Image}}|{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
    )
    if rc == 0 and "|" in info:
        image, nets_s = info.split("|", 1)
        net = next((n for n in nets_s.split() if n and n != sandbox), None)
        return image.strip(), net
    rc, row, _ = await _docker(
        "ps", "--no-trunc", "--filter", f"id={host}", "--format", "{{.Image}}|{{.Networks}}",
    )
    if rc == 0 and "|" in row:
        image, nets_s = row.split("|", 1)
        net = next(
            (n.strip() for n in nets_s.split(",") if n.strip() and n.strip() != sandbox), None
        )
        return image.strip(), net
    return "", None


async def _ensure_benchmark_network(name: str) -> bool:
    """Create the sealed benchmark network if it's missing — self-heal.

    NOTHING ELSE creates it. It's a top-level compose network that NO service
    attaches to (the read-proxy + benchmark/live-champion solvers attach
    imperatively via ``docker run --network=<name>``), and ``docker compose up``
    does NOT create a top-level network that no service uses — so a fresh host, a
    ``docker network prune``/``rm``, or a partial setup leaves it ABSENT. Every
    later ``docker run --network=<name>`` then fails "network not found": the
    read-proxy can't launch AND the live champion can't hot-swap, so the validator
    silently falls back to 100% burn (observed in prod — a follower stuck on burn
    for days traced to exactly this).

    Create it with the spec the validator compose declares: an ``--internal``
    bridge on ``172.30.0.0/24`` (subnet env-overridable via
    ``BENCHMARK_DOCKER_NETWORK_SUBNET``), so an untrusted solver can reach ONLY the
    ``.5`` block-pin proxy. Idempotent + best-effort: an already-present net (incl.
    a concurrent-create race) is success; a hard failure is logged and we proceed —
    the proxy launch still surfaces it loudly, exactly as before.
    """
    rc, _out, _err = await _docker("network", "inspect", name)
    if rc == 0:
        return True  # already exists — nothing to do
    subnet = os.environ.get("BENCHMARK_DOCKER_NETWORK_SUBNET", "172.30.0.0/24").strip()
    rc, _cid, err = await _docker(
        "network", "create", "--driver", "bridge", "--internal",
        "--subnet", subnet, name,
    )
    if rc == 0:
        logger.info(
            "[read-proxy] created missing benchmark network %s (internal bridge %s)",
            name, subnet,
        )
        return True
    # Lost a create race against another process? Re-check before declaring failure.
    rc2, _, _ = await _docker("network", "inspect", name)
    if rc2 == 0:
        return True
    logger.error(
        "[read-proxy] could NOT create benchmark network %s (%s) — proxy + solver "
        "launches will keep failing 'network not found' until it exists",
        name, err,
    )
    return False


async def ensure_read_proxy_container() -> bool:
    """Ensure the api ROUTES through the block-pin proxy, launching the managed container
    if it isn't already up. Idempotent; call once at api startup.

    CRITICAL ORDER: the env wiring (``SOLVER_READ_PROXY*``) is exported FIRST and is
    INDEPENDENT of any docker call. The proxy lives at a FIXED address, so a docker-socket
    failure (e.g. a 403 on self-inspect) must NEVER leave the api un-wired — that was the
    root cause of the repoint intermittency: the manager couldn't ``docker inspect`` itself,
    bailed BEFORE exporting, and every benchmark then read the un-pinned raw anvil instead
    of the proxy (silently pre-firewall; "no Web3" once the anvil was network-isolated).

    Returns True if the proxy is up + env wired, False if only the env could be wired
    (logged) — in which case a previously-launched proxy must already exist, else
    benchmarks fail loud (defer) rather than read the anvil.
    """
    if read_proxy_launch_disabled():
        logger.info("[read-proxy] DISABLE_READ_PROXY set — not wiring/launching the managed proxy")
        return False

    token = os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
    # (1) WIRE FIRST — unconditional, no docker call (fixed proxy address).
    _export_env(token)

    # (2) Resolve the api's OWN image (to launch/refresh the proxy from it) + minotaur net.
    image, minotaur_net = await _resolve_self_image_and_net()

    # (3) If the proxy is already running, leave it ONLY when it's on the api's current
    # image — so the proxy TRACKS the api (a consensus-relevant rewrite-table version bump
    # must not leave a stale proxy applying old rewrites). When we can't compare images
    # (inspect denied -> ''), fall back to leaving a running proxy alone (#301 robustness:
    # never block on the socket; a stale proxy is the rare, opt-in-only risk then).
    running, proxy_image = await _proxy_state()
    if running and (not image or not proxy_image or proxy_image == image):
        logger.info(
            "[read-proxy] env wired to %s; managed proxy running (%s)", PROXY_DATA_URL,
            "current image" if proxy_image and proxy_image == image else "image uncompared",
        )
        return True
    if not image:
        logger.error(
            "[read-proxy] env wired to %s but could NOT determine the api image to launch "
            "the proxy (docker inspect AND ps both failed) — a previously-launched proxy is "
            "required; otherwise benchmarks fail loud (defer), never read the anvil.",
            PROXY_DATA_URL,
        )
        return False
    if running:
        logger.info(
            "[read-proxy] proxy on stale image %s != api %s — recreating to track the api",
            proxy_image[:19], image[:19],
        )
    await _docker("rm", "-f", PROXY_CONTAINER_NAME)  # clear any stale/stopped instance
    sandbox = os.environ.get("BENCHMARK_DOCKER_NETWORK", "benchmark-sandbox").strip()
    # SELF-HEAL: nothing else creates this network (unused top-level compose net), so
    # create it if missing BEFORE the proxy — and, by extension, before the live champion
    # hot-swap — tries to attach. Without this, the run below fails "network not found"
    # and the validator silently burns 100%.
    await _ensure_benchmark_network(sandbox)
    # NOTE: no Watchtower label — the api owns this container's lifecycle: it recreates the
    # proxy from its OWN image when they diverge (api update) and leaves it otherwise.
    create = [
        "run", "-d", "--name", PROXY_CONTAINER_NAME, "--restart", "unless-stopped",
        "--network", sandbox, "--ip", PROXY_STATIC_IP,
        "-e", f"UPSTREAMS={_build_upstreams()}",
        "-e", f"CONTROL_TOKEN={token}",
        "-e", f"LISTEN_PORT={PROXY_PORT}",
        "-e", "LOG_LEVEL=INFO",
        image, "-m", _PROXY_MODULE,
    ]
    rc, _cid, err = await _docker(*create, timeout=90)
    if rc != 0:
        logger.error(
            "[read-proxy] env wired to %s but FAILED to launch the managed proxy: %s — "
            "benchmarks fail loud (defer) until a proxy is up; they never read the anvil.",
            PROXY_DATA_URL, err,
        )
        return False
    if minotaur_net:
        rcn, _, errn = await _docker("network", "connect", minotaur_net, PROXY_CONTAINER_NAME)
        if rcn != 0:
            logger.warning(
                "[read-proxy] could not attach %s to %s: %s",
                PROXY_CONTAINER_NAME, minotaur_net, errn,
            )
    logger.info(
        "[read-proxy] launched managed proxy %s on %s(.5)+%s from image %s; env wired to %s",
        PROXY_CONTAINER_NAME, sandbox, minotaur_net, image[:24], PROXY_DATA_URL,
    )
    return True
