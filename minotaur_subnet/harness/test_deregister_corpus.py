"""Consensus-safe app deregistration: a RETIRED deployment leaves the benchmark
corpus (historical draw + synthetic scenarios + pack-hash inputs) WITHOUT deleting
any order rows.

These exercise the shared seam so the runtime draw, the pack hash, and the veto
slice all derive the identical exclusion (see order_sampler.retired_app_chain_keys).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from minotaur_subnet.harness.benchmark_pack import (
    collect_synthetic_scenarios,
    deregistered_app_ids,
)
from minotaur_subnet.harness.order_sampler import (
    retired_app_chain_keys,
    sample_historical_orders,
)
from minotaur_subnet.store import AppIntentStore
from minotaur_subnet.shared.types import (
    AppIntentConfig,
    AppIntentDefinition,
    AppStatus,
    DeploymentResult,
)

CHAIN = 8453


def _store() -> tuple[AppIntentStore, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    return AppIntentStore(Path(tmp.name) / "s.db"), tmp


def _add_app(
    store: AppIntentStore,
    app_id: str,
    *,
    status: AppStatus,
    chain: int = CHAIN,
    scenarios: list | None = None,
) -> None:
    store.save_app(AppIntentDefinition(
        app_id=app_id, name=app_id, version="1.0.0", intent_type="swap",
        js_code="x", config=AppIntentConfig(supported_chains=[chain]),
        manifest={"benchmark_scenarios": scenarios or []},
    ))
    store.save_deployment(DeploymentResult(
        app_id=app_id, status=status, contract_address=f"0x{app_id}",
        chain_id=chain,
    ))


def _add_orders(store: AppIntentStore, app_id: str, n: int, chain: int = CHAIN) -> list[str]:
    ids = []
    for i in range(n):
        oid = f"{app_id}-{chain}-ord-{i}"
        store.save_order({
            "order_id": oid,
            "app_id": app_id,
            "status": "filled",
            "created_at": float(i),
            "chain_id": chain,
            # Distinct params so dedup never collapses across orders.
            "params": {"input_token": "0xA", "output_token": "0xB",
                       "input_amount": str(10 ** (i + 1))},
        })
        ids.append(oid)
    return ids


def test_retired_app_chain_keys_reports_retired_pairs():
    store, tmp = _store()
    try:
        _add_app(store, "live", status=AppStatus.ACTIVE)
        _add_app(store, "gone", status=AppStatus.RETIRED)
        assert retired_app_chain_keys(store) == {("gone", CHAIN)}
    finally:
        tmp.cleanup()


def test_retired_orders_dropped_from_draw_but_rows_kept():
    store, tmp = _store()
    try:
        _add_app(store, "v2", status=AppStatus.ACTIVE)
        _add_app(store, "v1", status=AppStatus.ACTIVE)
        v2_ids = set(_add_orders(store, "v2", 4))
        v1_ids = set(_add_orders(store, "v1", 4))

        before = {o["order_id"] for o in sample_historical_orders(store, "round-1")}
        assert v1_ids & before, "v1 orders should be drawn while operational"
        assert v2_ids & before

        # Deregister v1 (retire, don't delete).
        store.update_deployment_status("v1", CHAIN, AppStatus.RETIRED)

        after = {o["order_id"] for o in sample_historical_orders(store, "round-1")}
        assert not (v1_ids & after), "retired app's orders must leave the draw"
        assert v2_ids & after, "operational app's orders stay"

        # Rows are NOT deleted — still queryable in the store.
        assert {o["order_id"] for o in store.list_orders(app_id="v1")} == v1_ids
    finally:
        tmp.cleanup()


def test_draw_deterministic_for_fixed_retired_set():
    store, tmp = _store()
    try:
        _add_app(store, "v2", status=AppStatus.ACTIVE)
        _add_orders(store, "v2", 10)
        a = [o["order_id"] for o in sample_historical_orders(store, "round-x")]
        b = [o["order_id"] for o in sample_historical_orders(store, "round-x")]
        assert a == b
    finally:
        tmp.cleanup()


def test_explicit_exclude_overrides_auto_derivation():
    store, tmp = _store()
    try:
        _add_app(store, "v2", status=AppStatus.ACTIVE)
        ids = set(_add_orders(store, "v2", 5))
        # Force-exclude an operational app via the explicit param.
        drawn = {o["order_id"] for o in sample_historical_orders(
            store, "r", exclude_app_chains={("v2", CHAIN)})}
        assert not (ids & drawn)
    finally:
        tmp.cleanup()


def test_deregistered_apps_excluded_from_synthetic_but_drafts_kept():
    store, tmp = _store()
    try:
        _add_app(store, "live", status=AppStatus.ACTIVE,
                 scenarios=[{"name": "s1", "intent_function": "swap", "params": {}}])
        _add_app(store, "gone", status=AppStatus.RETIRED,
                 scenarios=[{"name": "s2", "intent_function": "swap", "params": {}}])
        _add_app(store, "draft", status=AppStatus.DRAFT,
                 scenarios=[{"name": "s3", "intent_function": "swap", "params": {}}])

        # Only the fully-retired app is deregistered. Drafts are left alone so the
        # deploy stays inert (only a deliberate retire changes the hash).
        assert deregistered_app_ids(store) == {"gone"}
        got = {s["app_id"] for s in collect_synthetic_scenarios(store)}
        assert got == {"live", "draft"}, "only deregistered (retired) apps drop"
    finally:
        tmp.cleanup()


def test_app_live_on_one_chain_not_deregistered():
    store, tmp = _store()
    try:
        store.save_app(AppIntentDefinition(
            app_id="multi", name="multi", version="1.0.0", intent_type="swap",
            js_code="x", config=AppIntentConfig(supported_chains=[CHAIN, 1]),
            manifest={"benchmark_scenarios": [
                {"name": "s", "intent_function": "swap", "params": {}}]},
        ))
        store.save_deployment(DeploymentResult(
            app_id="multi", status=AppStatus.ACTIVE, chain_id=CHAIN))
        store.save_deployment(DeploymentResult(
            app_id="multi", status=AppStatus.RETIRED, chain_id=1))
        # Retired on chain 1 but live on 8453 → NOT fully deregistered.
        assert deregistered_app_ids(store) == set()
        assert {s["app_id"] for s in collect_synthetic_scenarios(store)} == {"multi"}
    finally:
        tmp.cleanup()


def test_retiring_cutover_is_round_anchored():
    # A RETIRING deployment stays in the draw until its effective epoch, then drops —
    # driven purely by the opened_epoch parsed from round_id.
    store, tmp = _store()
    try:
        _add_app(store, "v2", status=AppStatus.ACTIVE)
        _add_app(store, "v1", status=AppStatus.ACTIVE)
        _add_orders(store, "v2", 4)
        v1_ids = set(_add_orders(store, "v1", 4))
        store.update_deployment_status("v1", CHAIN, AppStatus.RETIRING,
                                       retire_effective_epoch=100)

        # Round BEFORE the cutover (epoch 50): v1 still benchmarked.
        before = {o["order_id"] for o in sample_historical_orders(store, "round-e50-n1")}
        assert v1_ids & before, "RETIRING app stays in the draw before its cutover"

        # Round AT the cutover (epoch 100): v1 drops.
        at = {o["order_id"] for o in sample_historical_orders(store, "round-e100-n1")}
        assert not (v1_ids & at), "RETIRING app leaves the draw at its cutover epoch"

        # Rows are still present regardless.
        assert {o["order_id"] for o in store.list_orders(app_id="v1")} == v1_ids
    finally:
        tmp.cleanup()


def test_retiring_synthetic_cutover_is_round_anchored():
    store, tmp = _store()
    try:
        _add_app(store, "live", status=AppStatus.ACTIVE,
                 scenarios=[{"name": "s1", "intent_function": "swap", "params": {}}])
        _add_app(store, "closing", status=AppStatus.ACTIVE,
                 scenarios=[{"name": "s2", "intent_function": "swap", "params": {}}])
        store.update_deployment_status("closing", CHAIN, AppStatus.RETIRING,
                                       retire_effective_epoch=100)

        # Before cutover: still in the synthetic set + not yet deregistered.
        assert deregistered_app_ids(store, 50) == set()
        assert {s["app_id"] for s in collect_synthetic_scenarios(store, 50)} == {"live", "closing"}

        # At/after cutover: dropped.
        assert deregistered_app_ids(store, 100) == {"closing"}
        assert {s["app_id"] for s in collect_synthetic_scenarios(store, 100)} == {"live"}
    finally:
        tmp.cleanup()


def test_per_chain_retirement_is_precise():
    store, tmp = _store()
    try:
        # App live on 8453, retired on 1 — only chain-1 orders drop.
        store.save_app(AppIntentDefinition(
            app_id="multi", name="multi", version="1.0.0", intent_type="swap",
            js_code="x", config=AppIntentConfig(supported_chains=[CHAIN, 1]),
            manifest={"benchmark_scenarios": []},
        ))
        store.save_deployment(DeploymentResult(
            app_id="multi", status=AppStatus.ACTIVE, chain_id=CHAIN))
        store.save_deployment(DeploymentResult(
            app_id="multi", status=AppStatus.RETIRED, chain_id=1))
        base_ids = set(_add_orders(store, "multi", 4, chain=CHAIN))
        eth_ids = set(_add_orders(store, "multi", 4, chain=1))

        assert retired_app_chain_keys(store) == {("multi", 1)}
        drawn = {o["order_id"] for o in sample_historical_orders(store, "r")}
        assert base_ids & drawn
        assert not (eth_ids & drawn)
    finally:
        tmp.cleanup()
