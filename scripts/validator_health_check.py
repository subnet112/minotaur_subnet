#!/usr/bin/env python3
"""Validator health check + status report for subnet 112.

Produces two outputs on every run:

  ``summary.md``    — markdown status table covering every EVM in the
                      configured on-chain ValidatorRegistries. Pinned into
                      a tracking issue by the workflow so the latest
                      state is always one click away.
  ``findings.json`` — list of incident objects (stale weights, low Yuma
                      trust) the workflow opens GitHub issues for.

Exits 0 even when findings exist — the workflow inspects ``findings.json``
to decide which alerts to file. Exits non-zero only on a wiring error
(no registries configured, no subtensor reachable, etc.) so a broken
deploy surfaces as a red CI run instead of silent green.

Config (env vars):

  NETUID                       Subnet to inspect. Default 112.
  SUBTENSOR_NETWORK            Network arg to ``bt.Subtensor``. Default
                               "finney".
  STALE_THRESHOLD_SECONDS      Alert when last weight set is older than
                               this. Default 3600 (1 hour).
  LOW_TRUST_THRESHOLD          Alert when metagraph.trust < this.
                               Default 0.5.
  PROBE_TIMEOUT_SECONDS        Per-axon /identity HTTP timeout.
                               Default 5.
  REGISTRIES                   ``name|chain_id|rpc_url|registry_addr``
                               entries separated by commas. Order matters
                               only for the summary column order; chain
                               registration set is the union across all.

Example:

    REGISTRIES="Base|8453|https://mainnet.base.org|0x88a08d…,BT EVM|964|https://lite.chain.opentensor.ai|0x…"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


# ── Config ───────────────────────────────────────────────────────────────


NETUID = int(os.environ.get("NETUID", "112"))
SUBTENSOR_NETWORK = os.environ.get("SUBTENSOR_NETWORK", "finney")
STALE_THRESHOLD_SECONDS = int(os.environ.get("STALE_THRESHOLD_SECONDS", "3600"))
LOW_TRUST_THRESHOLD = float(os.environ.get("LOW_TRUST_THRESHOLD", "0.5"))
PROBE_TIMEOUT_SECONDS = float(os.environ.get("PROBE_TIMEOUT_SECONDS", "5"))
BLOCK_TIME_SECONDS = 12


REGISTRY_ABI = [
    {
        "name": "getValidators",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]


@dataclass
class Registry:
    name: str
    chain_id: int
    rpc_url: str
    address: str


@dataclass
class ValidatorStatus:
    """One row of the summary table.

    ``hotkey``, ``uid``, ``stake``, ``trust``, ``last_update_seconds_ago``
    are populated only when the EVM has been mapped to a metagraph entry
    via ``/identity`` cross-attestation. Registry-only entries (in-cluster
    validators with no metagraph hotkey) leave these fields as None and
    the summary renders them as ``—``.
    """

    evm_address: str
    chain_registrations: dict[str, bool] = field(default_factory=dict)
    hotkey: str | None = None
    uid: int | None = None
    stake: float | None = None
    axon_url: str = ""
    axon_published: bool = False
    identity_reachable: bool = False
    last_update_seconds_ago: int | None = None
    trust: float | None = None


def parse_registries() -> list[Registry]:
    raw = os.environ.get("REGISTRIES", "").strip()
    if not raw:
        return []
    out: list[Registry] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) != 4:
            print(
                f"[warn] skipping malformed REGISTRIES entry: {entry!r} "
                f"(expected name|chain_id|rpc_url|registry_addr)",
                file=sys.stderr,
            )
            continue
        name, chain_id, rpc_url, address = (p.strip() for p in parts)
        try:
            out.append(Registry(name=name, chain_id=int(chain_id),
                                rpc_url=rpc_url, address=address))
        except ValueError as exc:
            print(f"[warn] bad chain_id in {entry!r}: {exc}", file=sys.stderr)
    return out


# ── Registry reads ───────────────────────────────────────────────────────


def fetch_registered_evms(
    registries: list[Registry],
) -> dict[str, dict[str, bool]]:
    """Read getValidators() across all configured registries.

    Returns ``{evm_lower: {chain_name: registered_bool}}``. Every chain
    in ``registries`` appears as a key in each inner dict — False when
    the registry read succeeded but the EVM wasn't in the list, True
    when it was. A failed registry read drops that chain entirely from
    the inner dict (so the summary shows the column blank — operator can
    tell "failed to read" vs "explicitly absent").
    """
    from web3 import Web3

    union: dict[str, dict[str, bool]] = {}
    chain_names: list[str] = []
    chains_with_successful_read: set[str] = set()

    for reg in registries:
        chain_names.append(reg.name)
        try:
            w3 = Web3(Web3.HTTPProvider(
                reg.rpc_url,
                request_kwargs={"timeout": 10},
            ))
            c = w3.eth.contract(
                address=Web3.to_checksum_address(reg.address),
                abi=REGISTRY_ABI,
            )
            evms = [v.lower() for v in c.functions.getValidators().call()]
            chains_with_successful_read.add(reg.name)
        except Exception as exc:
            print(
                f"[warn] failed to read registry {reg.name} at {reg.address}: {exc}",
                file=sys.stderr,
            )
            continue
        for evm in evms:
            union.setdefault(evm, {})[reg.name] = True

    # Fill in explicit False for chains that did get read but didn't list this EVM
    for evm in union:
        for chain in chains_with_successful_read:
            union[evm].setdefault(chain, False)
    return union


# ── Metagraph + identity discovery ───────────────────────────────────────


async def probe_identity(
    session: aiohttp.ClientSession,
    axon_url: str,
) -> dict | None:
    """Fetch /identity at the axon. Returns None on any failure."""
    url = axon_url.rstrip("/") + "/identity"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
        ) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


async def discover_identity_map(metagraph) -> dict[str, dict]:
    """Probe every axon-serving UID for /identity. Returns
    ``{evm_lower: {hotkey, axon_url, uid}}``."""
    candidates: list[tuple[int, str, str]] = []
    for uid, ax in enumerate(metagraph.axons):
        if ax.ip != "0.0.0.0" and ax.port != 0:
            candidates.append((uid, metagraph.hotkeys[uid], f"http://{ax.ip}:{ax.port}"))

    if not candidates:
        return {}

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(probe_identity(session, url) for _, _, url in candidates),
        )

    out: dict[str, dict] = {}
    for (uid, hk, url), data in zip(candidates, results):
        if data is None:
            continue
        evm = (data.get("evm_address") or "").lower()
        if not evm:
            continue
        out[evm] = {"hotkey": hk, "axon_url": url, "uid": uid}
    return out


def build_statuses(
    registered_evms: dict[str, dict[str, bool]],
    identity_map: dict[str, dict],
    metagraph,
    current_block: int,
    all_chain_names: list[str],
) -> list[ValidatorStatus]:
    """Combine the registry union with the identity-map / metagraph view."""
    out: list[ValidatorStatus] = []
    for evm, chain_regs in sorted(registered_evms.items()):
        # Backfill any chain that wasn't in the per-EVM map at all — happens
        # when a registry read failed; render blank in the summary.
        for chain in all_chain_names:
            chain_regs.setdefault(chain, None)  # type: ignore[arg-type]

        s = ValidatorStatus(evm_address=evm, chain_registrations=dict(chain_regs))

        info = identity_map.get(evm)
        if info is not None:
            uid = info["uid"]
            s.hotkey = info["hotkey"]
            s.uid = uid
            s.axon_url = info["axon_url"]
            s.axon_published = True
            s.identity_reachable = True
            s.stake = float(metagraph.stake[uid])
            # ``validator_trust`` is the post-Yuma trust score (how well
            # this UID's vote aligns with network consensus). Distinct
            # from ``trust`` (which doesn't exist on Metagraph in current
            # bittensor versions). Healthy validators sit ~0.95+; values
            # below ~0.5 indicate a divergent voter being penalized.
            s.trust = float(metagraph.validator_trust[uid])
            last_update_block = int(metagraph.last_update[uid])
            s.last_update_seconds_ago = (
                max(0, current_block - last_update_block) * BLOCK_TIME_SECONDS
            )
        out.append(s)
    return out


# ── Issue detection ──────────────────────────────────────────────────────


def detect_findings(statuses: list[ValidatorStatus]) -> list[dict]:
    """Return alert payloads for stale-weights / low-trust conditions.

    EVMs without a discoverable hotkey are skipped — they're either
    in-cluster validators (which don't post weights to Bittensor) or
    registered-but-not-yet-deployed operators. Both states are reflected
    in the summary; neither is an "incident".
    """
    findings: list[dict] = []
    for s in statuses:
        if s.uid is None:
            continue  # not a metagraph validator — nothing to alert on

        if (
            s.last_update_seconds_ago is not None
            and s.last_update_seconds_ago > STALE_THRESHOLD_SECONDS
        ):
            findings.append({
                "type": "stale_weights",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "axon_url": s.axon_url,
                "details": (
                    f"No weight update for {s.last_update_seconds_ago // 60} min "
                    f"(threshold {STALE_THRESHOLD_SECONDS // 60} min). "
                    f"Validator may be down, rate-limited, or its weight-emitter "
                    f"has crashed."
                ),
            })

        if s.trust is not None and s.trust < LOW_TRUST_THRESHOLD:
            findings.append({
                "type": "low_trust",
                "validator_evm": s.evm_address,
                "hotkey": s.hotkey,
                "uid": s.uid,
                "axon_url": s.axon_url,
                "details": (
                    f"Yuma trust {s.trust:.3f} < threshold {LOW_TRUST_THRESHOLD:.2f}. "
                    f"This validator's weight vector diverges from consensus and "
                    f"is being penalized (reduced dividends). Check what it's "
                    f"voting on and why it differs from the rest of the network."
                ),
            })

    return findings


# ── Markdown summary ─────────────────────────────────────────────────────


def _short(s: str, head: int = 6, tail: int = 4) -> str:
    if len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _fmt_seconds_ago(secs: int | None) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def _fmt_check(val: bool | None) -> str:
    if val is None:
        return "·"  # unknown — registry read failed
    return "✅" if val else "❌"


def _fmt_trust(t: float | None) -> str:
    if t is None:
        return "—"
    marker = "✅" if t >= LOW_TRUST_THRESHOLD else "⚠️"
    return f"{t:.3f} {marker}"


def render_summary(
    statuses: list[ValidatorStatus],
    findings: list[dict],
    *,
    chain_names: list[str],
    current_block: int,
    netuid: int,
) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    header = ["EVM", "Hotkey", "UID", "Stake (TAO)", "Last weights", "Trust", "Axon", "/identity"]
    header.extend(chain_names)
    sep = ["---"] * len(header)

    lines: list[str] = []
    lines.append("# Validator Health Status")
    lines.append("")
    lines.append(f"_Last updated: **{ts}**  ·  netuid={netuid}  ·  block={current_block:,}_")
    lines.append("")
    lines.append(f"## Registered validators ({len(statuses)})")
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(sep) + " |")

    for s in statuses:
        row = [
            f"`{_short(s.evm_address)}`",
            f"`{_short(s.hotkey or '—', head=8, tail=4)}`" if s.hotkey else "_(in-cluster)_",
            str(s.uid) if s.uid is not None else "—",
            f"{s.stake:,.0f}" if s.stake is not None else "—",
            _fmt_seconds_ago(s.last_update_seconds_ago),
            _fmt_trust(s.trust),
            _fmt_check(s.axon_published) if s.uid is not None else "—",
            _fmt_check(s.identity_reachable) if s.uid is not None else "—",
        ]
        for chain in chain_names:
            row.append(_fmt_check(s.chain_registrations.get(chain)))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append("## Active alerts")
    lines.append("")
    if not findings:
        lines.append("_None._")
    else:
        for f in findings:
            kind = {"stale_weights": "Stale weights", "low_trust": "Low Yuma trust"}.get(
                f["type"], f["type"],
            )
            lines.append(
                f"- **{kind}** — `{_short(f['validator_evm'])}` "
                f"(uid={f['uid']}, hk=`{_short(f['hotkey'] or '?', 8, 4)}`)\n"
                f"  {f['details']}"
            )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `.github/workflows/validator-health.yml` "
        "(runs every 15 min). Configuration: `STALE_THRESHOLD_SECONDS="
        f"{STALE_THRESHOLD_SECONDS}`, `LOW_TRUST_THRESHOLD={LOW_TRUST_THRESHOLD}`._"
    )
    return "\n".join(lines) + "\n"


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    registries = parse_registries()
    if not registries:
        print(
            "ERROR: no registries configured. Set REGISTRIES env "
            "(format: name|chain_id|rpc_url|registry_addr, comma-separated)",
            file=sys.stderr,
        )
        return 2

    print(
        f"[info] checking netuid={NETUID} on {SUBTENSOR_NETWORK} "
        f"across {len(registries)} registries: "
        f"{', '.join(r.name for r in registries)}",
        file=sys.stderr,
    )

    registered = fetch_registered_evms(registries)
    print(f"[info] {len(registered)} unique EVMs registered across all chains",
          file=sys.stderr)

    # Lazy import — bittensor is heavy. If it fails to import we'd rather
    # error here than at module load time.
    try:
        import bittensor as bt
    except ImportError as exc:
        print(f"ERROR: bittensor import failed: {exc}", file=sys.stderr)
        return 2

    try:
        sub = bt.Subtensor(network=SUBTENSOR_NETWORK)
        metagraph = sub.metagraph(netuid=NETUID)
        current_block = int(sub.get_current_block())
    except Exception as exc:
        print(f"ERROR: subtensor query failed: {exc}", file=sys.stderr)
        return 2

    identity_map = asyncio.run(discover_identity_map(metagraph))
    print(
        f"[info] identity-mapped {len(identity_map)} of "
        f"{sum(1 for ax in metagraph.axons if ax.ip != '0.0.0.0' and ax.port != 0)} "
        f"axon-serving UIDs",
        file=sys.stderr,
    )

    statuses = build_statuses(
        registered_evms=registered,
        identity_map=identity_map,
        metagraph=metagraph,
        current_block=current_block,
        all_chain_names=[r.name for r in registries],
    )

    findings = detect_findings(statuses)

    summary_md = render_summary(
        statuses=statuses,
        findings=findings,
        chain_names=[r.name for r in registries],
        current_block=current_block,
        netuid=NETUID,
    )
    with open("summary.md", "w") as f:
        f.write(summary_md)

    with open("findings.json", "w") as f:
        json.dump({
            "findings": findings,
            "generated_at": int(time.time()),
            "netuid": NETUID,
            "current_block": current_block,
        }, f, indent=2)

    print(
        f"[info] wrote summary.md ({len(summary_md)} bytes) + "
        f"findings.json ({len(findings)} findings)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
