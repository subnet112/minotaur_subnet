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


def test_proxy_running_current_image_left_alone(monkeypatch):
    # inspect works: proxy running on the api's CURRENT image -> leave it, env wired.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # _resolve_self -> api image
        (0, "true|sha256:img", ""),                         # _proxy_state -> running, SAME image
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)        # no relaunch
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL
    assert os.environ["SOLVER_READ_PROXY_TOKEN"] == "tok"


def test_proxy_stale_image_recreated(monkeypatch):
    # inspect works: proxy running on a STALE image -> recreate so it tracks the api.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:NEW|minotaur benchmark-sandbox ", ""),  # api image NEW
        (0, "true|sha256:OLD", ""),                         # proxy running OLD image
        (0, "", ""),                                        # rm -f
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("rm", "-f") for c in fake.calls)   # removed stale
    assert any(c and c[0] == "run" for c in fake.calls)     # recreated on the new image


def test_env_wired_inspect_403_running_proxy_left(monkeypatch):
    # inspect 403s for BOTH self + proxy -> ps fallbacks (api image via ps; proxy running,
    # image UNcompared) -> leave it. env wired throughout (the #301 robustness path).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),                                          # inspect self -> 403
        (0, "ghcr.io/x:latest|production_minotaur,benchmark-sandbox", ""),  # self ps fallback
        (1, "", "403 Forbidden"),                                          # inspect proxy -> 403
        (0, "minotaur-rpc-pin-proxy", ""),                                 # proxy ps -> running
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)       # left alone (uncompared)
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL


def test_env_wired_even_when_all_docker_fails(monkeypatch):
    # ROOT-CAUSE FIX: every docker call 403s + proxy not running -> can't launch (False),
    # BUT the env is STILL wired -> the api never silently reads the raw anvil.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),   # inspect self -> 403
        (1, "", "403 Forbidden"),   # self ps fallback -> 403  (api image = "")
        (1, "", "403 Forbidden"),   # inspect proxy -> 403
        (1, "", "403 Forbidden"),   # proxy ps -> 403  (running=False)
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # env wired (THE FIX)


def test_launch_when_absent(monkeypatch):
    # proxy not running, inspect works -> launch from the api image.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok123")
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    fake = FakeDocker([
        (0, "sha256:apiimg|minotaur benchmark-sandbox ", ""),  # api image
        (1, "", "No such object"),                             # proxy inspect -> absent
        (0, "", ""),                                           # proxy ps -> empty (not running)
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
    # self-inspect 403s -> ps fallback resolves the api image + net; proxy not running -> launch.
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (1, "", "403 Forbidden"),                                          # inspect self -> 403
        (0, "ghcr.io/x/img:latest|production_minotaur,benchmark-sandbox", ""),  # self ps fallback
        (1, "", "No such object"),                                         # proxy inspect -> absent
        (0, "", ""),                                                       # proxy ps -> not running
        (0, "", ""),                                                       # rm -f
        (0, "cid", ""),                                                    # run
        (0, "", ""),                                                       # network connect
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
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),   # api image
        (1, "", "No such object"),                           # proxy inspect -> absent
        (0, "", ""),                                          # proxy ps -> not running
        (0, "", ""),                                          # rm -f
        (1, "", "Address already in use"),                   # run FAILS
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # wired despite launch fail
