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
        (0, "[{}]", ""),                                     # network inspect -> net EXISTS (no create)
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("rm", "-f") for c in fake.calls)   # removed stale
    assert any(c and c[0] == "run" for c in fake.calls)     # recreated on the new image
    assert not any(c[:2] == ("network", "create") for c in fake.calls)  # net present -> no create


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
        (0, "[{}]", ""),                                       # network inspect -> exists
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
        (0, "[{}]", ""),                                                   # network inspect -> exists
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
        (0, "[{}]", ""),                                      # network inspect -> exists (no create)
        (1, "", "Address already in use"),                   # run FAILS
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is False
    assert os.environ["SOLVER_READ_PROXY"] == rpm.PROXY_DATA_URL  # wired despite launch fail


# ── benchmark-sandbox network self-heal (the unused-top-level-net gap) ────────

def test_ensure_benchmark_network_noop_when_present(monkeypatch):
    _clear_env(monkeypatch)
    fake = FakeDocker([(0, "[{}]", "")])  # network inspect -> exists
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True
    assert not any(c[:2] == ("network", "create") for c in fake.calls)  # never creates


def test_ensure_benchmark_network_creates_when_missing(monkeypatch):
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "network benchmark-sandbox not found"),  # inspect -> MISSING
        (0, "netid", ""),                                # create -> ok
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True
    creates = [c for c in fake.calls if c[:2] == ("network", "create")]
    assert creates, "should have created the missing network"
    args = creates[0]
    assert "--internal" in args and "--subnet" in args
    assert "172.30.0.0/24" in args and args[-1] == "benchmark-sandbox"


def test_ensure_benchmark_network_subnet_env_override(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BENCHMARK_DOCKER_NETWORK_SUBNET", "10.9.0.0/24")
    fake = FakeDocker([(1, "", "missing"), (0, "netid", "")])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("bench")) is True
    creates = [c for c in fake.calls if c[:2] == ("network", "create")]
    assert "10.9.0.0/24" in creates[0]


def test_ensure_benchmark_network_create_race_then_present(monkeypatch):
    # create loses a race (already exists) -> re-inspect finds it -> success, not failure.
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "missing"),          # inspect -> missing
        (1, "", "already exists"),   # create -> fails (concurrent create)
        (0, "netid", ""),            # re-inspect -> now present
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is True


def test_ensure_benchmark_network_hard_fail_returns_false(monkeypatch):
    # create fails AND still missing -> False (logged, best-effort), never raises.
    _clear_env(monkeypatch)
    fake = FakeDocker([
        (1, "", "missing"),  # inspect -> missing
        (1, "", "boom"),     # create -> hard fail
        (1, "", "missing"),  # re-inspect -> still missing
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm._ensure_benchmark_network("benchmark-sandbox")) is False


def test_launch_path_self_heals_missing_network(monkeypatch):
    # The proxy-launch path creates the missing net BEFORE the run (the prod fix).
    _clear_env(monkeypatch)
    monkeypatch.setenv("SOLVER_ROUND_INTERNAL_API_KEY", "tok")
    fake = FakeDocker([
        (0, "sha256:img|minotaur benchmark-sandbox ", ""),  # api image
        (1, "", "no such container"),                       # _proxy_state inspect -> not running
        (1, "", "no such container"),                       # _proxy_state ps fallback -> not running
        (0, "", ""),                                        # rm -f
        (1, "", "network not found"),                       # network inspect -> MISSING
        (0, "netid", ""),                                   # network create -> ok
        (0, "cid", ""),                                     # run
        (0, "", ""),                                        # network connect
    ])
    monkeypatch.setattr(rpm, "_docker", fake)
    assert asyncio.run(rpm.ensure_read_proxy_container()) is True
    assert any(c[:2] == ("network", "create") for c in fake.calls)   # self-healed
    # ordering: the create must precede the proxy run
    order = [c for c in fake.calls if c[:2] == ("network", "create") or (c and c[0] == "run")]
    assert order[0][:2] == ("network", "create") and order[1][0] == "run"
