"""Tests for the LIVE champion keyless+metered RPC proxy path and the
live-network internal-only guard.

The live champion historically got the KEYED ``BASE_RPC_URL`` (env + init_cfg)
and ran on a non-internal net — a hostile champion could read the provider API
key and exfiltrate user order data. This suite pins the hardening:

  - ``solver_read_proxy.live_read_proxy_config()`` is OPT-IN + FAIL-SAFE.
  - ``DockerRuntimeSolver`` resets the proxy budget PER ORDER (BLIND-3 metering)
    and closes the session on shutdown, only when the feature is on.
  - ``orchestrator``'s live-net guard warns by default and hard-fails on an
    explicitly non-internal net only when ``LIVE_SOLVER_REQUIRE_INTERNAL=1``.

Unit-level: no Docker daemon, no proxy container — the orchestrator's
``start_docker`` and the proxy control calls are stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from minotaur_subnet.harness import solver_read_proxy as srp
from minotaur_subnet.harness import orchestrator as orch
from minotaur_subnet.harness.runtime_solver import DockerRuntimeSolver
from minotaur_subnet.sdk.intent_solver import MarketSnapshot, SolverMetadata


# ── live_read_proxy_config: opt-in + fail-safe ──────────────────────────────


def test_live_config_disabled_by_default(monkeypatch):
    """Feature ships INERT: no env => None => champion keeps direct RPC."""
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    monkeypatch.delenv("SOLVER_LIVE_RPC_PROXY", raising=False)
    assert srp.live_read_proxy_config() is None
    assert srp.live_rpc_via_proxy_enabled() is False


def test_live_config_fail_safe_when_url_missing(monkeypatch):
    """Enabled but the proxy hasn't exported its live URL yet => None (fail-safe:
    never point the champion at an unreachable proxy)."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.delenv("SOLVER_LIVE_RPC_PROXY", raising=False)
    assert srp.live_rpc_via_proxy_enabled() is True
    assert srp.live_read_proxy_config() is None


def test_live_config_resolves_keyless_and_metered(monkeypatch):
    """Enabled + URL present => a keyless, budget-enforced config; the URLs the
    solver dials carry NO API key (they point at the proxy)."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_CONTROL", "http://minotaur-rpc-pin-proxy:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "tok")
    monkeypatch.delenv("LIVE_SOLVER_RPC_BUDGET", raising=False)
    cfg = srp.live_read_proxy_config()
    assert cfg is not None
    assert cfg.url == "http://172.30.1.5:8645"
    assert cfg.control_url == "http://minotaur-rpc-pin-proxy:8645"
    assert cfg.token == "tok"
    assert cfg.budget == srp.DEFAULT_LIVE_RPC_BUDGET > 0
    sid = srp.new_live_session_id()
    assert sid.startswith(f"{srp.LIVE_PROXY_SESSION_PREFIX}-")
    assert sid != srp.new_live_session_id()  # unique per runtime, never a fixed name
    url = srp.proxy_rpc_url(cfg, sid, 8453)
    assert url == f"http://172.30.1.5:8645/rpc/{sid}/base"
    assert "alchemy" not in url and "key" not in url  # keyless


def test_live_config_budget_override(monkeypatch):
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")
    monkeypatch.setenv("LIVE_SOLVER_RPC_BUDGET", "12345")
    assert srp.live_read_proxy_config().budget == 12345


def test_live_config_negative_budget_falls_back(monkeypatch):
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")
    monkeypatch.setenv("LIVE_SOLVER_RPC_BUDGET", "-5")
    assert srp.live_read_proxy_config().budget == srp.DEFAULT_LIVE_RPC_BUDGET


# ── DockerRuntimeSolver: per-order metering + session lifecycle ─────────────


_SID = "live-testsess1"  # a per-runtime id as create() would mint


def _rt(*, live_cfg=None, session=None) -> DockerRuntimeSolver:
    session = session or MagicMock()
    session._closed = False
    session.quote = AsyncMock(return_value="q")
    session.generate_plan = AsyncMock(return_value="p")
    session.shutdown = AsyncMock()
    return DockerRuntimeSolver(
        session=session,
        image_ref="ghcr.io/test/solver:latest",
        metadata=SolverMetadata(name="t", version="0", author="t", description=""),
        chain_ids=[8453],
        rpc_urls={8453: f"http://172.30.1.5:8645/rpc/{_SID}/base"},
        live_proxy_cfg=live_cfg,
        live_proxy_session_id=(_SID if live_cfg else None),
    )


def _intent_state_snapshot():
    intent = MagicMock(); intent.app_id = "app_test"
    state = MagicMock(); state.chain_id = 8453
    return intent, state, MarketSnapshot.empty(chain_id=8453)


@pytest.mark.asyncio
async def test_per_order_reset_when_proxy_on():
    """BLIND-3: with the live proxy on, the budget is reset before EACH order."""
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock(return_value=True)) as reset:
        await rt.generate_plan(intent, state, snap)
        await rt.quote(intent, state, snap)
    assert reset.await_count == 2  # one per order op
    reset.assert_awaited_with(cfg, _SID)


@pytest.mark.asyncio
async def test_failed_reset_reopens_session():
    """A failed reset means the session may be GONE (proxy restart dropped its
    in-memory registry → reads silently fall to the anon UNMETERED bucket). The
    runtime must re-open the session (blocks={}, head reads) to restore
    enforcement rather than stay degraded until the next api restart."""
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock(return_value=False)), \
         patch.object(srp, "open_session", new=AsyncMock()) as reopen:
        await rt.generate_plan(intent, state, snap)
    reopen.assert_awaited_once_with(cfg, _SID, {})


@pytest.mark.asyncio
async def test_failed_reset_and_reopen_never_fail_the_order():
    """Metering is best-effort: even reset AND re-open both failing must not
    block the order itself."""
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock(return_value=False)), \
         patch.object(srp, "open_session", new=AsyncMock(side_effect=OSError("proxy down"))):
        assert await rt.generate_plan(intent, state, snap) == "p"


@pytest.mark.asyncio
async def test_no_reset_when_proxy_off():
    """Feature off => no control-plane calls at all (zero overhead / inert)."""
    rt = _rt(live_cfg=None)
    intent, state, snap = _intent_state_snapshot()
    with patch.object(srp, "reset_session", new=AsyncMock()) as reset:
        await rt.generate_plan(intent, state, snap)
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_closes_session_when_proxy_on():
    cfg = MagicMock()
    rt = _rt(live_cfg=cfg)
    with patch.object(srp, "close_session", new=AsyncMock()) as close:
        await rt.shutdown()
    close.assert_awaited_once_with(cfg, _SID)


@pytest.mark.asyncio
async def test_shutdown_no_close_when_proxy_off():
    rt = _rt(live_cfg=None)
    with patch.object(srp, "close_session", new=AsyncMock()) as close:
        await rt.shutdown()
    close.assert_not_awaited()


# ── create(): opens a metered HEAD session + routes RPC keylessly ───────────


@pytest.mark.asyncio
async def test_create_wires_keyless_proxy(monkeypatch):
    """With the feature on, create() opens a HEAD (blocks={}) session and forwards
    KEYLESS proxy URLs to the container (rpc_overrides), never the keyed URL."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "tok")
    monkeypatch.setenv("LIVE_SOLVER_NETWORK", "live-solver")

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)

    captured = {}

    async def _open(cfg, sid, blocks):
        captured["open"] = (cfg, sid, blocks)
        return {}

    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=_open), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        rt = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453],
            rpc_urls={8453: "https://base-mainnet.g.alchemy.com/v2/SECRETKEY"},
        )

    # HEAD session opened with an empty pin (latest reads), metered, on a
    # freshly-minted per-runtime id.
    cfg, sid, blocks = captured["open"]
    assert blocks == {} and cfg.budget > 0
    assert sid.startswith(f"{srp.LIVE_PROXY_SESSION_PREFIX}-")
    assert rt._live_proxy_session_id == sid
    # start_docker got KEYLESS proxy URLs as rpc_overrides — the SECRETKEY never
    # reaches the container via env.
    kwargs = start.await_args.kwargs
    assert kwargs["rpc_overrides"] == {8453: f"http://172.30.1.5:8645/rpc/{sid}/base"}
    assert "SECRETKEY" not in str(kwargs["rpc_overrides"])
    # init_cfg rpc_urls were rewritten to the keyless proxy URL too.
    init_cfg = fake_session.initialize.await_args.args[0]
    assert init_cfg["rpc_urls"][8453] == f"http://172.30.1.5:8645/rpc/{sid}/base"
    assert rt._live_proxy_cfg is not None


@pytest.mark.asyncio
async def test_create_session_ids_unique_per_runtime(monkeypatch):
    """Two runtimes never share a session id: during a hot-swap the displaced
    champion's shutdown() close_session must not close the session its successor
    just opened (which would silently un-meter the new champion)."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")

    def _fake_session():
        s = MagicMock()
        s.initialize = AsyncMock()
        s.shutdown = AsyncMock()
        s.metadata = AsyncMock(
            return_value=SolverMetadata(name="c", version="1", author="a", description="")
        )
        return s

    with patch.object(orch.SolverOrchestrator, "start_docker",
                      new=AsyncMock(side_effect=[_fake_session(), _fake_session()])), \
         patch.object(srp, "open_session", new=AsyncMock(return_value={})), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        rt_old = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:old", chain_ids=[8453], rpc_urls={8453: "https://x/KEY"},
        )
        rt_new = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:new", chain_ids=[8453], rpc_urls={8453: "https://x/KEY"},
        )
    assert rt_old._live_proxy_session_id != rt_new._live_proxy_session_id

    # The displaced runtime's shutdown closes ITS OWN session only.
    with patch.object(srp, "close_session", new=AsyncMock()) as close:
        await rt_old.shutdown()
    close.assert_awaited_once_with(rt_old._live_proxy_cfg, rt_old._live_proxy_session_id)


@pytest.mark.asyncio
async def test_create_keeps_local_chains_off_the_proxy(monkeypatch):
    """31337 (local Anvil) shares the 'eth' proxy slug — routing it through the
    proxy would repoint the champion's LOCAL chain at Ethereum mainnet. Local
    chains keep their direct RPC URL; only real chains are proxied."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")

    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)
    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=AsyncMock(return_value={})), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453, 31337],
            rpc_urls={8453: "https://x/KEY", 31337: "http://anvil:8545"},
        )
    overrides = start.await_args.kwargs["rpc_overrides"]
    assert set(overrides) == {8453}  # 31337 NOT proxied
    init_cfg = fake_session.initialize.await_args.args[0]
    assert init_cfg["rpc_urls"][31337] == "http://anvil:8545"  # untouched


@pytest.mark.asyncio
async def test_create_fails_safe_to_direct_rpc_when_disabled(monkeypatch):
    """Feature off => no session opened, keyed URL used as today, no rpc_overrides."""
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)
    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=AsyncMock(side_effect=AssertionError("must not open"))), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        rt = await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453],
            rpc_urls={8453: "https://base-mainnet.g.alchemy.com/v2/SECRETKEY"},
        )
    assert rt._live_proxy_cfg is None
    assert start.await_args.kwargs["rpc_overrides"] is None


# ── orchestrator: live-net internal-only guard ──────────────────────────────


def test_require_internal_env_gating(monkeypatch):
    for v, expect in [("1", True), ("true", True), ("on", True), ("0", False), ("", False)]:
        monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", v)
        assert orch._require_internal_live_net() is expect
    monkeypatch.delenv("LIVE_SOLVER_REQUIRE_INTERNAL", raising=False)
    assert orch._require_internal_live_net() is False


@pytest.mark.asyncio
async def test_docker_network_is_internal_none_for_missing_net():
    """A network that can't be inspected => None ('unknown', never crash)."""
    assert await orch._docker_network_is_internal("no-such-net-xyz-123") is None


@pytest.mark.asyncio
async def test_start_docker_refuses_noninternal_live_net_when_required(monkeypatch):
    """live=True + an EXISTING but non-internal net + REQUIRE=1 => refuse to start."""
    monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", "1")
    o = orch.SolverOrchestrator()
    with patch.object(orch, "_docker_network_exists", new=AsyncMock(return_value=True)), \
         patch.object(orch, "_docker_network_is_internal", new=AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="not a Docker --internal net"):
            await o.start_docker("ghcr.io/test/solver:latest", live=True, network="prod-bridge")


# ── decoupling: proxy net is independent of the legacy LIVE_SOLVER_NETWORK ───


def test_live_proxy_network_independent_of_legacy(monkeypatch):
    """The 2026-07-22 root cause: the proxy net must NOT be derived from the
    legacy LIVE_SOLVER_NETWORK (which the leader sets to production_minotaur),
    else enabling the feature collides with the direct-RPC champion's net."""
    monkeypatch.setenv("LIVE_SOLVER_NETWORK", "production_minotaur")
    monkeypatch.delenv("LIVE_SOLVER_PROXY_NETWORK", raising=False)
    assert srp.live_proxy_network() == "live-solver"
    monkeypatch.setenv("LIVE_SOLVER_PROXY_NETWORK", "custom-net")
    assert srp.live_proxy_network() == "custom-net"


@pytest.mark.asyncio
async def test_create_champion_lands_on_proxy_net_despite_legacy_env(monkeypatch):
    """Enabling the feature must land the champion on the PROXY net even when the
    legacy LIVE_SOLVER_NETWORK is set to the leader's production_minotaur — this
    is the exact config that stranded the champion before the decoupling."""
    monkeypatch.setenv("LIVE_SOLVER_RPC_VIA_PROXY", "1")
    monkeypatch.setenv("SOLVER_LIVE_RPC_PROXY", "http://172.30.1.5:8645")
    monkeypatch.setenv("LIVE_SOLVER_NETWORK", "production_minotaur")  # legacy, must be ignored
    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)
    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch.object(srp, "open_session", new=AsyncMock(return_value={})), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest",
            chain_ids=[8453],
            rpc_urls={8453: "https://x/KEY"},
        )
    assert start.await_args.kwargs["network"] == "live-solver"  # NOT production_minotaur


@pytest.mark.asyncio
async def test_create_champion_uses_legacy_net_when_feature_off(monkeypatch):
    """Feature OFF => champion stays on the legacy LIVE_SOLVER_NETWORK (today's
    leader behavior), and the keyed RPC is used (fail-safe, orders keep flowing)."""
    monkeypatch.delenv("LIVE_SOLVER_RPC_VIA_PROXY", raising=False)
    monkeypatch.setenv("LIVE_SOLVER_NETWORK", "production_minotaur")
    fake_session = MagicMock()
    fake_session.initialize = AsyncMock()
    fake_session.metadata = AsyncMock(
        return_value=SolverMetadata(name="c", version="1", author="a", description="")
    )
    start = AsyncMock(return_value=fake_session)
    with patch.object(orch.SolverOrchestrator, "start_docker", new=start), \
         patch("minotaur_subnet.harness.runtime_solver._reap_orphan_live_solvers", lambda: None):
        await DockerRuntimeSolver.create(
            image_ref="ghcr.io/test/solver:latest", chain_ids=[8453], rpc_urls={8453: "https://x/KEY"},
        )
    assert start.await_args.kwargs["network"] == "production_minotaur"
    assert start.await_args.kwargs["rpc_overrides"] is None  # keyed RPC path


# ── existence guard: never launch a live champion onto a missing net ────────


@pytest.mark.asyncio
async def test_start_docker_refuses_missing_live_net(monkeypatch):
    """live=True + a DEFINITELY-absent net => refuse (the 2026-07-22 failure mode:
    a doomed container launched onto a net nothing had created)."""
    o = orch.SolverOrchestrator()
    with patch.object(orch, "_docker_network_exists", new=AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="does not exist"):
            await o.start_docker("img:latest", live=True, network="live-solver")


@pytest.mark.asyncio
async def test_start_docker_proceeds_when_net_existence_unknown(monkeypatch):
    """A can't-determine (None) existence must NOT hard-fail — proceed and let
    docker run report any real error (inspect can be denied behind socket-proxy)."""
    monkeypatch.delenv("LIVE_SOLVER_REQUIRE_INTERNAL", raising=False)
    o = orch.SolverOrchestrator()
    with patch.object(orch, "_docker_network_exists", new=AsyncMock(return_value=None)), \
         patch.object(orch, "_docker_network_is_internal", new=AsyncMock(return_value=None)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=RuntimeError("reached-docker-run"))):
        with pytest.raises(RuntimeError, match="reached-docker-run"):
            await o.start_docker("img:latest", live=True, network="live-solver")


@pytest.mark.asyncio
async def test_docker_network_exists_classification():
    """_docker_network_exists: rc0 => True; 'not found' stderr => False;
    other non-zero => None (unknown, never 'absent')."""
    async def _mk(rc, err=b""):
        p = MagicMock()
        p.returncode = rc
        p.communicate = AsyncMock(return_value=(b"live-solver", err))
        return p
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=await _mk(0))):
        assert await orch._docker_network_exists("live-solver") is True
    with patch("asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=await _mk(1, b"Error: No such network: x"))):
        assert await orch._docker_network_exists("x") is False
    with patch("asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=await _mk(1, b"403 permission denied"))):
        assert await orch._docker_network_exists("x") is None


@pytest.mark.asyncio
async def test_start_docker_no_guard_for_benchmark(monkeypatch):
    """Benchmark (live=False) never triggers the live-net guard, even non-internal."""
    monkeypatch.setenv("LIVE_SOLVER_REQUIRE_INTERNAL", "1")
    o = orch.SolverOrchestrator()
    check = AsyncMock(return_value=False)
    with patch.object(orch, "_docker_network_is_internal", new=check), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=RuntimeError("stop-before-docker"))):
        # live=False => guard skipped; it proceeds to the (stubbed) docker call.
        with pytest.raises(RuntimeError, match="stop-before-docker"):
            await o.start_docker("img:latest", live=False, network="bench-sandbox")
    check.assert_not_awaited()
