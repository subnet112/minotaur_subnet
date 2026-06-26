"""Unit tests for the api-launched block-pin proxy manager (read_proxy_manager)."""
import asyncio
import os

from minotaur_subnet.api import read_proxy_manager as rpm


class FakeDocker:
    """Async stand-in for rpm._docker: records calls, returns scripted (rc, out, err)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, *args, timeout=30.0):
        self.calls.append(args)
        return self.responses.pop(0) if self.responses else (0, "", "")


def _clear_env(monkeypatch):
    for k in (
        "DISABLE_READ_PROXY", "SOLVER_READ_PROXY", "SOLVER_READ_PROXY_CONTROL",
        "SOLVER_READ_PROXY_TOKEN", "BENCHMARK_DOCKER_NETWORK",
    ):
        monkeypatch.delenv(k, raising=False)


def test_disabled_skips_docker(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DISABLE_READ_PROXY", "1")
    fake = FakeDocker([])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert fake.calls == []  # never touched docker
    assert os.environ.get("SOLVER_READ_PROXY") is None


def test_env_wired_when_proxy_already_running(monkeypatch):
    # The lead's steady state: proxy up → `docker ps` confirms → env wired, no launch.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([(0, "minotaur-rpc-pin-proxy", "")])  # _proxy_is_running -> present
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)        # no relaunch
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL   # env WIRED
    assert os.environ["SOLVER_READ_PROXY_TOKEN"] == "tok"


def test_env_wired_even_when_docker_inspect_403(monkeypatch):
    # ROOT-CAUSE FIX: proxy not running AND both inspect + ps-fallback fail (socket-proxy
    # 403) -> the env is STILL exported. The api routes to a previously-launched proxy /
    # fails loud — it NEVER silently falls back to the raw anvil (the repoint intermittency).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "", ""),                # _proxy_is_running: docker ps -> empty (not running)
        (1, "", "403 Forbidden"),   # docker inspect self -> 403
        (1, "", "403 Forbidden"),   # docker ps id-fallback -> also denied
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False    # couldn't launch
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL      # but env IS wired (THE FIX)


def test_launch_when_absent_inspect_works(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok123")
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    fake = FakeDocker([
        (0, "", ""),                                           # not running
        (0, "sha256:apiimg|minotaur benchmark-sandbox ", ""),  # inspect self OK
        (0, "", ""),                                           # rm -f
        (0, "cid", ""),                                        # run
        (0, "", ""),                                           # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    run = next(c for c in fake.calls if c and c[0] == "run")
    assert rpm.PROXY_CONTAINER_NAME in run and rpm.PROXY_STATIC_IP in run
    assert "sha256:apiimg" in run and rpm._PROXY_MODULE in run
    blob = " ".join(x for c in fake.calls for x in c)
    assert "CONTROL_TOKEN=tok123" in blob and "base=https://base.example" in blob
    assert any(c[:2] == ("network", "connect") and "minotaur" in c for c in fake.calls)
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_launch_via_ps_fallback_when_inspect_403(monkeypatch):
    # inspect-by-id 403s -> fall back to `docker ps` to resolve the api image + net.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "", ""),                                  # not running
        (1, "", "403 Forbidden"),                     # inspect self -> 403
        (0, "ghcr.io/x/img:latest|production_minotaur,benchmark-sandbox", ""),  # ps fallback
        (0, "", ""),                                  # rm -f
        (0, "cid", ""),                               # run
        (0, "", ""),                                  # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    run = next(c for c in fake.calls if c and c[0] == "run")
    assert "ghcr.io/x/img:latest" in run              # image from the ps fallback
    assert any(c[:2] == ("network", "connect") and "production_minotaur" in c for c in fake.calls)
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_create_failure_still_wires_env(monkeypatch):
    # launch FAILS -> env STILL wired (prior proxy used / benchmarks fail loud, never anvil).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "", ""),                                          # not running
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),   # inspect self OK
        (0, "", ""),                                          # rm -f
        (1, "", "Address already in use"),                   # run FAILS
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # wired despite launch fail
