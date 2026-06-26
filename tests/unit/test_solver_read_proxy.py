"""Unit tests for the orchestrator-side block-pin proxy client wiring."""
from __future__ import annotations

import asyncio

from minotaur_subnet.harness import solver_read_proxy as srp


def test_read_proxy_config_disabled(monkeypatch):
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    assert srp.read_proxy_config() is None


def test_read_proxy_config_defaults(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645/")  # trailing slash
    monkeypatch.delenv("SOLVER_READ_PROXY_CONTROL", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY_CHAINS", raising=False)
    cfg = srp.read_proxy_config()
    assert cfg.url == "http://p:8645"  # stripped
    assert cfg.control_url == "http://p:8645"  # defaults to the data url
    assert cfg.token == ""
    assert cfg.chain_ids == (8453,)  # Base anchor default


def test_read_proxy_config_control_url_split(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://172.30.0.5:8645")  # solver/data (sandbox)
    monkeypatch.setenv("SOLVER_READ_PROXY_CONTROL", "http://rpc-pin-proxy:8645/")  # api/control
    cfg = srp.read_proxy_config()
    assert cfg.url == "http://172.30.0.5:8645"  # solver dials this
    assert cfg.control_url == "http://rpc-pin-proxy:8645"  # api dials this (stripped)


def test_read_proxy_config_explicit(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_TOKEN", "sekret")
    monkeypatch.setenv("SOLVER_READ_PROXY_CHAINS", "8453,1")
    cfg = srp.read_proxy_config()
    assert cfg.token == "sekret"
    assert cfg.chain_ids == (8453, 1)


def test_build_pin_blocks_only_routed_chains_present_in_map():
    cfg = srp.ReadProxyConfig(url="http://p", control_url="http://p", token="", chain_ids=(8453,))
    rpc_map = {8453: "u_base", 1: "u_eth"}
    # only routed (8453) chains in rpc_map are pinned, at fork_block; eth (not
    # routed) is left out
    assert srp.build_pin_blocks(cfg, rpc_map, 12345) == {"base": 12345}


def test_build_pin_blocks_empty_when_routed_chain_absent():
    cfg = srp.ReadProxyConfig(url="http://p", control_url="http://p", token="", chain_ids=(8453,))
    assert srp.build_pin_blocks(cfg, {1: "u_eth"}, 12345) == {}


def test_proxy_rpc_url_uses_chain_name():
    cfg = srp.ReadProxyConfig(url="http://p:8645", control_url="http://p:8645", token="", chain_ids=(8453,))
    assert srp.proxy_rpc_url(cfg, "s1", 8453) == "http://p:8645/rpc/s1/base"
    assert srp.proxy_rpc_url(cfg, "s1", 1) == "http://p:8645/rpc/s1/eth"
    assert srp.proxy_rpc_url(cfg, "s1", 964) == "http://p:8645/rpc/s1/btevm"


def test_pack_hash_block_rewrite_gating(monkeypatch):
    from minotaur_subnet.harness.rpc_budget_proxy.rewrite_table import rewrite_table_record
    # NOTE: round_anchored_pin_enabled() is DEFAULT ON (unset == pinned); only
    # {0,false,no,off} disable it.
    # no proxy -> None regardless of pin (byte-identical to a non-proxy fleet)
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    assert srp.pack_hash_block_rewrite() is None
    # proxy + pin (default-on) -> the rewrite record (reads route through proxy)
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    assert srp.pack_hash_block_rewrite() == rewrite_table_record()
    # proxy but pin EXPLICITLY off -> None (run_benchmark won't route w/o fork_block)
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    assert srp.pack_hash_block_rewrite() is None
    # pin back on but no proxy -> None
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    assert srp.pack_hash_block_rewrite() is None


# ── Deterministic compute-budget: config parsing ──────────────────────────────


def test_read_proxy_config_budget_unset_is_zero(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.delenv("SOLVER_READ_PROXY_BUDGET", raising=False)
    cfg = srp.read_proxy_config()
    assert cfg.budget == 0  # default (observe; inert as a cutoff)


def test_read_proxy_config_budget_parsed(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    assert srp.read_proxy_config().budget == 2000


def test_read_proxy_config_budget_invalid_is_zero(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "not-an-int")
    assert srp.read_proxy_config().budget == 0


def test_read_proxy_config_budget_negative_is_zero(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "-5")
    assert srp.read_proxy_config().budget == 0


# ── budget_enforced gating ────────────────────────────────────────────────────


def test_budget_enforced_false_without_proxy(monkeypatch):
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    assert srp.budget_enforced() is False  # no proxy -> inert


def test_budget_enforced_false_when_budget_zero(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.delenv("SOLVER_READ_PROXY_BUDGET", raising=False)
    assert srp.budget_enforced() is False  # proxy but no budget -> observe


def test_budget_enforced_true_when_proxy_and_budget(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    assert srp.budget_enforced() is True


# ── open_session body: budget + mode only when enforcing ──────────────────────


def _capture_control_post(monkeypatch):
    """Patch srp._control_post to capture (path, body) and return a stub record."""
    captured: dict = {}

    def _fake(cfg, path, body, timeout=10.0):
        captured["path"] = path
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(srp, "_control_post", _fake)
    return captured


def test_open_session_includes_budget_and_mode_when_enforcing(monkeypatch):
    captured = _capture_control_post(monkeypatch)
    cfg = srp.ReadProxyConfig(
        url="http://p", control_url="http://p", token="", chain_ids=(8453,), budget=2000
    )
    asyncio.run(srp.open_session(cfg, "s1", {"base": 123}))
    assert captured["path"] == "/control/open"
    assert captured["body"]["session_id"] == "s1"
    assert captured["body"]["blocks"] == {"base": 123}
    assert captured["body"]["budget"] == 2000
    assert captured["body"]["mode"] == "enforce"


def test_open_session_omits_budget_when_not_enforcing(monkeypatch):
    captured = _capture_control_post(monkeypatch)
    cfg = srp.ReadProxyConfig(
        url="http://p", control_url="http://p", token="", chain_ids=(8453,), budget=0
    )
    asyncio.run(srp.open_session(cfg, "s1", {"base": 123}))
    assert "budget" not in captured["body"]  # proxy defaults to observe
    assert "mode" not in captured["body"]
    assert captured["body"]["blocks"] == {"base": 123}


def test_reset_session_posts_session_id(monkeypatch):
    captured = _capture_control_post(monkeypatch)
    cfg = srp.ReadProxyConfig(
        url="http://p", control_url="http://p", token="", chain_ids=(8453,), budget=2000
    )
    asyncio.run(srp.reset_session(cfg, "s1"))
    assert captured["path"] == "/control/reset"
    assert captured["body"] == {"session_id": "s1"}  # no blocks -> pin preserved


def test_reset_session_swallows_errors(monkeypatch):
    def _boom(cfg, path, body, timeout=10.0):
        raise RuntimeError("proxy down")

    monkeypatch.setattr(srp, "_control_post", _boom)
    cfg = srp.ReadProxyConfig(
        url="http://p", control_url="http://p", token="", chain_ids=(8453,), budget=2000
    )
    # Must NOT raise — a failed reset cannot abort the benchmark.
    asyncio.run(srp.reset_session(cfg, "s1"))


# ── pack_hash_compute_budget gating (mirrors pack_hash_block_rewrite) ──────────


def test_pack_hash_compute_budget_gating(monkeypatch):
    from minotaur_subnet.harness.rpc_budget_proxy.cost_table import compute_budget_record

    # no proxy -> None regardless of budget/pin
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    assert srp.pack_hash_compute_budget() is None

    # proxy + pin (default-on) but budget 0 -> None (observe, not the cutoff)
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.delenv("SOLVER_READ_PROXY_BUDGET", raising=False)
    assert srp.pack_hash_compute_budget() is None

    # proxy + pin (default-on) + budget>0 -> the budget record (it's the cutoff)
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    assert srp.pack_hash_compute_budget() == compute_budget_record(2000)

    # proxy + budget>0 but pin EXPLICITLY off -> None (reads won't route)
    monkeypatch.setenv("ROUND_ANCHORED_PIN", "0")
    assert srp.pack_hash_compute_budget() is None

    # pin back on but no proxy -> None
    monkeypatch.delenv("ROUND_ANCHORED_PIN", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    assert srp.pack_hash_compute_budget() is None


# ── generate_plan_recv_timeout: default when off, backstop when enforcing ─────


def test_generate_plan_recv_timeout_default_when_budget_off(monkeypatch):
    monkeypatch.delenv("SOLVER_READ_PROXY", raising=False)
    monkeypatch.delenv("SOLVER_READ_PROXY_BUDGET", raising=False)
    assert srp.generate_plan_recv_timeout(30.0) == 30.0  # inert


def test_generate_plan_recv_timeout_backstop_when_enforcing(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    monkeypatch.delenv("GENERATE_PLAN_BACKSTOP_SECONDS", raising=False)
    out = srp.generate_plan_recv_timeout(30.0)
    assert out == 300.0  # loosened to the default backstop
    assert out > 30.0


def test_generate_plan_recv_timeout_backstop_never_below_default(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    monkeypatch.setenv("GENERATE_PLAN_BACKSTOP_SECONDS", "10")  # smaller than default
    assert srp.generate_plan_recv_timeout(30.0) == 30.0  # max(default, backstop)


def test_generate_plan_recv_timeout_custom_backstop(monkeypatch):
    monkeypatch.setenv("SOLVER_READ_PROXY", "http://p:8645")
    monkeypatch.setenv("SOLVER_READ_PROXY_BUDGET", "2000")
    monkeypatch.setenv("GENERATE_PLAN_BACKSTOP_SECONDS", "600")
    assert srp.generate_plan_recv_timeout(30.0) == 600.0
