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


def test_self_inspect_failure_exports_nothing(monkeypatch):
    _clear_env(monkeypatch)
    fake = FakeDocker([(1, "", "Cannot connect to the Docker daemon")])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    # fail-safe: no env -> read path stays unwired -> benchmark fails loud, not mis-score
    assert os.environ.get("SOLVER_READ_PROXY") is None


def test_launch_when_absent(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok123")
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example")
    monkeypatch.setenv("ETH_RPC_URL", "https://eth.example")
    fake = FakeDocker([
        (0, "sha256:apiimg|minotaur benchmark-sandbox ", ""),  # self-inspect
        (1, "", "No such object: minotaur-rpc-pin-proxy"),       # proxy absent
        (0, "newcontainerid", ""),                               # run
        (0, "", ""),                                             # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True

    run = next(c for c in fake.calls if c and c[0] == "run")
    assert "--name" in run and rpm.PROXY_CONTAINER_NAME in run
    assert "--ip" in run and rpm.PROXY_STATIC_IP in run
    assert "--network" in run and "benchmark-sandbox" in run
    assert "sha256:apiimg" in run                       # the api's OWN image
    assert "-m" in run and rpm._PROXY_MODULE in run     # launches the proxy module
    blob = " ".join(x for c in fake.calls for x in c)
    assert "CONTROL_TOKEN=tok123" in blob               # reuses the existing key
    assert "base=https://base.example" in blob          # UPSTREAMS from the api's RPCs
    assert any(c[:2] == ("network", "connect") and "minotaur" in c for c in fake.calls)
    # env exported for the existing read_proxy_config wiring
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL
    assert os.environ["SOLVER_READ_PROXY_CONTROL"] == rpm.PROXY_CONTROL_URL
    assert os.environ["SOLVER_READ_PROXY_TOKEN"] == "tok123"


def test_idempotent_when_running_current_image(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # self-inspect
        (0, "true|sha256:img", ""),                         # proxy: running, SAME image
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert not any(c and c[0] == "run" for c in fake.calls)   # left alone, no recreate
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # but still exports env


def test_recreate_when_stale_image(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:NEW|minotaur benchmark-sandbox ", ""),  # self-inspect (NEW image)
        (0, "true|sha256:OLD", ""),                         # proxy: running OLD image
        (0, "", ""),                                        # rm -f
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("rm", "-f") for c in fake.calls)   # removed stale
    assert any(c and c[0] == "run" for c in fake.calls)     # recreated on the new image


def test_create_failure_exports_nothing(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # self-inspect
        (1, "", "No such object"),                          # proxy absent
        (1, "", "Address already in use"),                  # run FAILS
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ.get("SOLVER_READ_PROXY") is None      # fail-safe: nothing exported
