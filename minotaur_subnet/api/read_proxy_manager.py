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
import json
import logging
import os
import re
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

# LIVE champion path (opt-in; see solver_read_proxy.live_read_proxy_config). A
# DEDICATED --internal net so the adopted champion reaches ONLY the proxy — not
# the internet, relayer, IMDS, or docker-socket-proxy. The proxy is attached to it
# at a static IP; the champion (on LIVE_SOLVER_NETWORK) dials that IP KEYLESSLY.
from minotaur_subnet.harness.solver_read_proxy import LIVE_SOLVER_NETWORK_DEFAULT

LIVE_SOLVER_NETWORK_NAME = (
    os.environ.get("LIVE_SOLVER_NETWORK", LIVE_SOLVER_NETWORK_DEFAULT).strip()
    or LIVE_SOLVER_NETWORK_DEFAULT
)
LIVE_SOLVER_NETWORK_SUBNET = os.environ.get(
    "LIVE_SOLVER_NETWORK_SUBNET", "172.31.0.0/24"
).strip()
PROXY_LIVE_IP = os.environ.get("SOLVER_LIVE_RPC_PROXY_IP", "172.31.0.5").strip()
PROXY_LIVE_DATA_URL = f"http://{PROXY_LIVE_IP}:{PROXY_PORT}"

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
    """``UPSTREAMS`` from the api's live read RPCs (the same upstreams the anvils fork).

    ``slug=url`` for each real (non-local) wired chain, sourced from the chain
    registry so the sidecar's UPSTREAMS keys stay in lockstep with the proxy
    route slugs (``solver_read_proxy.CHAIN_NAMES``) — the two used to be
    independently hand-maintained and could silently drift.
    """
    from minotaur_subnet.chains import registry

    parts: list[str] = []
    for cid in registry.wired_chain_ids():
        s = registry.spec(cid)
        if s is None or s.is_local:  # local Anvil (31337) shares the "eth" slug; skip
            continue
        url = registry.proxy_upstream(cid)
        if url:
            parts.append(f"{s.slug}={url}")
    return ",".join(parts)


def _export_env(token: str) -> None:
    """Point the existing ``read_proxy_config`` wiring at the launched proxy.

    ``setdefault`` so an operator who pinned an explicit URL (e.g. a custom lead
    topology) always wins; a bare validator gets the managed defaults.
    """
    os.environ.setdefault("SOLVER_READ_PROXY", PROXY_DATA_URL)
    os.environ.setdefault("SOLVER_READ_PROXY_CONTROL", PROXY_CONTROL_URL)
    if token:
        os.environ.setdefault("SOLVER_READ_PROXY_TOKEN", token)


def _rpc_proxy_env() -> dict[str, str]:
    """The api's ``RPC_PROXY_*`` env, forwarded verbatim into the managed proxy.

    This is the proxy's operator-tunable surface (e.g. the
    ``RPC_PROXY_RESPONSE_CACHE=0`` cache kill switch, upstream concurrency,
    cache bounds) — settable on the API service without an image change.
    """
    return {k: v for k, v in os.environ.items() if k.startswith("RPC_PROXY_")}


async def _proxy_state() -> tuple[bool, str, dict[str, str] | None]:
    """``(running?, image, rpc_env)`` of the managed proxy.

    Tries ``docker inspect`` (precise image + env — so the proxy can be RECREATED when
    the api image changes, e.g. a consensus-relevant rewrite-table version bump, OR when
    the operator flips an ``RPC_PROXY_*`` var on the api; needs the docker CLI's API
    version pinned via ``DOCKER_API_VERSION``, else the unpinned CLI's version
    negotiation gets a 403 from the socket-proxy). Falls back to ``docker ps``
    (running-only, image ``""``, env ``None`` = uncomparable) where inspect is denied —
    ``GET /containers/json`` is allowed even where ``GET /containers/<id>/json`` 403s.
    Never blocks on the socket.
    """
    rc, out, _ = await _docker(
        "inspect", PROXY_CONTAINER_NAME,
        "--format", "{{.State.Running}}|{{.Image}}|{{json .Config.Env}}",
    )
    if rc == 0 and "|" in out:
        running, image, env_json = (out.split("|", 2) + ["", ""])[:3]
        rpc_env: dict[str, str] | None = None
        try:
            entries = json.loads(env_json) or []
            rpc_env = dict(
                e.split("=", 1) for e in entries
                if isinstance(e, str) and "=" in e and e.startswith("RPC_PROXY_")
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass  # env uncomparable; image comparison still works
        return running.strip() == "true", image.strip(), rpc_env
    rc, names, _ = await _docker(
        "ps", "--filter", f"name=^{PROXY_CONTAINER_NAME}$", "--format", "{{.Names}}",
    )
    return (rc == 0 and PROXY_CONTAINER_NAME in names.split()), "", None


# Where the api's REAL container id is read from (module constants for tests).
_MOUNTINFO_PATH = "/proc/self/mountinfo"
_CGROUP_PATH = "/proc/self/cgroup"
_CONTAINER_ID_RE = re.compile(r"([0-9a-f]{64})")


def _self_container_id() -> str:
    """The api container's REAL id — do NOT trust the hostname for this.

    Watchtower recreates containers by CLONING the old container's config,
    which bakes the OLD container's id in as an explicit ``Hostname``. After
    any watchtower update, ``socket.gethostname()`` therefore names a DEAD
    container: self-inspect 403/404s, ``ps --filter id=`` matches nothing, and
    the manager can never resolve its own image again (observed live
    2026-07-02 — every ensure attempt, including #492's background retries,
    stayed "uncomparable" and a stale proxy persisted). Compose-driven
    recreates get a fresh id-hostname, which is why this only bites after
    watchtower updates.

    The kernel knows the truth: the container's /etc/hostname bind mount in
    ``/proc/self/mountinfo`` (and, on cgroup-v1 hosts, ``/proc/self/cgroup``)
    carries the real 64-hex id. Fall back to the hostname only when neither
    yields one (non-container dev runs).
    """
    for path in (_MOUNTINFO_PATH, _CGROUP_PATH):
        try:
            with open(path) as fh:
                for line in fh:
                    m = _CONTAINER_ID_RE.search(line)
                    if m:
                        return m.group(1)
        except OSError:
            continue
    return socket.gethostname()


async def _resolve_self_image_and_net() -> tuple[str, str | None]:
    """The api's own image + its minotaur/validator network (to launch the proxy from the
    same image, attached to the same egress net).

    Identifies itself by the REAL container id (see :func:`_self_container_id`),
    then tries the precise ``docker inspect self``; falls back to ``docker ps``
    where the socket-proxy denies inspect-by-id (403). Returns ("", None) if
    both fail.
    """
    host = _self_container_id()
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


async def _ensure_internal_network(name: str, subnet: str) -> bool:
    """Create ``name`` as an ``--internal`` bridge on ``subnet`` if missing.

    Generic sibling of :func:`_ensure_benchmark_network` used for the dedicated
    live-solver net. Idempotent + best-effort (an already-present net, incl. a
    concurrent-create race, is success).
    """
    rc, _o, _e = await _docker("network", "inspect", name)
    if rc == 0:
        return True
    rc, _c, err = await _docker(
        "network", "create", "--driver", "bridge", "--internal", "--subnet", subnet, name,
    )
    if rc == 0:
        logger.info("[read-proxy] created internal network %s (%s)", name, subnet)
        return True
    rc2, _, _ = await _docker("network", "inspect", name)  # lost a create race?
    if rc2 == 0:
        return True
    logger.error(
        "[read-proxy] could NOT create internal network %s (%s): %s", name, subnet, err
    )
    return False


async def _attach_proxy_to_live_net() -> None:
    """Attach the managed proxy to the dedicated live-solver ``--internal`` net and
    export ``SOLVER_LIVE_RPC_PROXY`` so the live champion routes RPC through it.

    Best-effort and FAIL-SAFE: on ANY failure (net absent, proxy container not up
    yet, connect denied) we do NOT export the URL, so the live champion keeps its
    existing RPC path rather than being pointed at an unreachable proxy. Only a
    confirmed attach turns the feature on. ``setdefault`` respects an operator who
    pinned a custom live proxy URL.
    """
    if not await _ensure_internal_network(LIVE_SOLVER_NETWORK_NAME, LIVE_SOLVER_NETWORK_SUBNET):
        logger.error(
            "[read-proxy] live-solver net %s absent — live RPC proxy stays OFF",
            LIVE_SOLVER_NETWORK_NAME,
        )
        return
    rc, _out, err = await _docker(
        "network", "connect", "--ip", PROXY_LIVE_IP,
        LIVE_SOLVER_NETWORK_NAME, PROXY_CONTAINER_NAME,
    )
    already = "already" in (err or "").lower()  # already attached == idempotent OK
    if rc != 0 and not already:
        logger.error(
            "[read-proxy] could NOT attach proxy to live-solver net %s (%s) — "
            "live RPC proxy stays OFF (champion keeps direct RPC)",
            LIVE_SOLVER_NETWORK_NAME, err,
        )
        return
    os.environ.setdefault("SOLVER_LIVE_RPC_PROXY", PROXY_LIVE_DATA_URL)
    logger.info(
        "[read-proxy] proxy attached to live-solver net %s(%s); live RPC proxy ON (%s)",
        LIVE_SOLVER_NETWORK_NAME, PROXY_LIVE_IP,
        os.environ.get("SOLVER_LIVE_RPC_PROXY"),
    )


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


# Background self-heal cadence for a DEGRADED ensure (docker resolution failed or
# was uncomparable). Observed live 2026-07-02: during a watchtower rolling update
# two consecutive api boots each failed the ONE-SHOT docker resolve (transient
# socket-proxy/DNS churn), first leaving a stale proxy "uncompared", then — after
# the stale one was removed — no proxy at all, with no recovery until a human
# stepped in. ~10 retries x 30s rides out that churn window.
_ENSURE_RETRY_ATTEMPTS = 10
_ENSURE_RETRY_DELAY_SECONDS = 30.0
_ensure_retry_task: asyncio.Task | None = None


def _schedule_ensure_retry() -> None:
    """Spawn (at most one) background task re-running the ensure until it is
    fully healthy — proxy running, image AND env actually compared current —
    or the attempt budget runs out. Benchmarks fail loud (defer) meanwhile."""
    global _ensure_retry_task
    if _ensure_retry_task is not None and not _ensure_retry_task.done():
        return

    async def _loop() -> None:
        for attempt in range(1, _ENSURE_RETRY_ATTEMPTS + 1):
            await asyncio.sleep(_ENSURE_RETRY_DELAY_SECONDS)
            try:
                ok, degraded = await _ensure_impl()
            except Exception as exc:  # noqa: BLE001 - keep retrying through raise
                logger.warning("[read-proxy] ensure retry %d raised: %s", attempt, exc)
                continue
            if not degraded:
                logger.info(
                    "[read-proxy] ensure retry %d recovered (proxy %s)",
                    attempt, "up" if ok else "disabled",
                )
                return
        # INFO, not ERROR: on hosts where the socket-proxy permanently denies
        # inspect (#301 fallback), "uncomparable" is a steady state, not a fault.
        logger.info(
            "[read-proxy] ensure retries exhausted (%d) — proxy state still "
            "uncomparable/unresolved; a stale proxy may persist until the next "
            "api restart with healthy docker access",
            _ENSURE_RETRY_ATTEMPTS,
        )

    _ensure_retry_task = asyncio.get_running_loop().create_task(_loop())


async def ensure_read_proxy_container() -> bool:
    """Ensure the api ROUTES through the block-pin proxy, launching the managed container
    if it isn't already up. Idempotent; call once at api startup.

    Returns True if the proxy is up + env wired, False if only the env could be wired
    (logged) — in which case a previously-launched proxy must already exist, else
    benchmarks fail loud (defer) rather than read the anvil.

    A DEGRADED result (docker resolution failed or the running proxy couldn't be
    compared) additionally schedules a bounded background retry — a boot-time
    docker/socket-proxy race (e.g. a watchtower rolling update) must not strand
    the proxy unmanaged for the api's whole lifetime.
    """
    ok, degraded = await _ensure_impl()
    if degraded:
        _schedule_ensure_retry()
    # Opt-in (LIVE_SOLVER_RPC_VIA_PROXY): attach the proxy to the dedicated
    # live-solver internal net so the adopted champion routes RPC through it
    # (keyless + metered). Idempotent + fail-safe — see _attach_proxy_to_live_net.
    from minotaur_subnet.harness.solver_read_proxy import live_rpc_via_proxy_enabled
    if live_rpc_via_proxy_enabled():
        await _attach_proxy_to_live_net()
    return ok


async def _ensure_impl() -> tuple[bool, bool]:
    """One ensure pass. Returns ``(ok, degraded)``.

    ``ok`` mirrors the public contract (proxy up + env wired). ``degraded`` is True
    when docker state could not be (fully) resolved — self-image unresolved, launch
    failed, or a running proxy left in place UNCOMPARED — i.e. the cases a later
    retry can genuinely improve on.

    CRITICAL ORDER: the env wiring (``SOLVER_READ_PROXY*``) is exported FIRST and is
    INDEPENDENT of any docker call. The proxy lives at a FIXED address, so a docker-socket
    failure (e.g. a 403 on self-inspect) must NEVER leave the api un-wired — that was the
    root cause of the repoint intermittency: the manager couldn't ``docker inspect`` itself,
    bailed BEFORE exporting, and every benchmark then read the un-pinned raw anvil instead
    of the proxy (silently pre-firewall; "no Web3" once the anvil was network-isolated).
    """
    if read_proxy_launch_disabled():
        logger.info("[read-proxy] DISABLE_READ_PROXY set — not wiring/launching the managed proxy")
        return False, False

    token = os.environ.get("SOLVER_ROUND_INTERNAL_API_KEY", "").strip()
    # (1) WIRE FIRST — unconditional, no docker call (fixed proxy address).
    _export_env(token)

    # (2) Resolve the api's OWN image (to launch/refresh the proxy from it) + minotaur net.
    image, minotaur_net = await _resolve_self_image_and_net()

    # (3) If the proxy is already running, leave it ONLY when it's on the api's current
    # image — so the proxy TRACKS the api (a consensus-relevant rewrite-table version bump
    # must not leave a stale proxy applying old rewrites) — AND its RPC_PROXY_* env matches
    # the api's (so an operator flipping e.g. RPC_PROXY_RESPONSE_CACHE=0 takes effect on
    # the next api restart, no manual container surgery). When we can't compare (inspect
    # denied -> ''/None), fall back to leaving a running proxy alone (#301 robustness:
    # never block on the socket; a stale proxy is the rare, opt-in-only risk then).
    running, proxy_image, proxy_rpc_env = await _proxy_state()
    compared = bool(image and proxy_image) and proxy_rpc_env is not None
    image_current = not image or not proxy_image or proxy_image == image
    env_current = proxy_rpc_env is None or proxy_rpc_env == _rpc_proxy_env()
    if running and image_current and env_current:
        logger.info(
            "[read-proxy] env wired to %s; managed proxy running (%s)", PROXY_DATA_URL,
            "current image" if compared else "image/env uncompared",
        )
        # Uncompared = a stale proxy may be in place; degraded so a retry re-checks.
        return True, not compared
    if not image:
        logger.error(
            "[read-proxy] env wired to %s but could NOT determine the api image to launch "
            "the proxy (docker inspect AND ps both failed) — a previously-launched proxy is "
            "required; otherwise benchmarks fail loud (defer), never read the anvil.",
            PROXY_DATA_URL,
        )
        return False, True
    if running:
        if not image_current:
            logger.info(
                "[read-proxy] proxy on stale image %s != api %s — recreating to track the api",
                proxy_image[:19], image[:19],
            )
        else:
            logger.info(
                "[read-proxy] proxy RPC_PROXY_* env %s != api %s — recreating to apply it",
                proxy_rpc_env, _rpc_proxy_env(),
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
    ]
    # Pin-cache disk persistence (opt-in): when the operator sets the persist
    # path (auto-forwarded as an RPC_PROXY_* env by the loop below), mount a
    # named volume for its directory so the snapshot survives this rm+run
    # recreate — the whole point is to skip the cold re-fetch storm after an
    # api update. Inert (no volume) when the path is unset. The path should live
    # under /var/cache/pin-proxy — the image pre-chowns that dir to the non-root
    # runtime user (uid 1000), so the fresh named volume inherits a writable
    # mount point (a root-owned volume would make the snapshot write silently
    # no-op). See the Dockerfile + rpc_budget_proxy/_persist.py.
    pin_persist = os.environ.get("RPC_PROXY_PIN_CACHE_PERSIST_PATH", "").strip()
    if pin_persist:
        pin_vol = os.environ.get("RPC_PROXY_PIN_CACHE_VOLUME", "minotaur-pin-cache").strip()
        pin_dir = os.path.dirname(pin_persist) or "/var/cache/pin-proxy"
        create += ["-v", f"{pin_vol}:{pin_dir}"]
    for k, v in sorted(_rpc_proxy_env().items()):  # operator-tunable proxy knobs
        create += ["-e", f"{k}={v}"]
    create += [image, "-m", _PROXY_MODULE]
    rc, _cid, err = await _docker(*create, timeout=90)
    if rc != 0:
        logger.error(
            "[read-proxy] env wired to %s but FAILED to launch the managed proxy: %s — "
            "benchmarks fail loud (defer) until a proxy is up; they never read the anvil.",
            PROXY_DATA_URL, err,
        )
        return False, True
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
    return True, False
