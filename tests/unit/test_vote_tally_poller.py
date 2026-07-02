"""Fleet vote-tally poller (scripts/tally_independent_votes.py).

Groups each validator's published independent vote by candidate and decides
WOULD-ADOPT vs quorum. This is the shadow evidence that the quorum adopts good
challengers and rejects bad ones with adoption off.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import tally_independent_votes as T  # noqa: E402


def _p(url, role, vote, cand="cand_1"):
    return {
        "url": url, "ok": True,
        # NEW independent_vote shape: relative better/worse/matched/compared
        # counts replaced the retired scalar chal_score/champ_score aggregates.
        "vote": {"candidate_id": cand, "role": role, "vote": vote,
                 "better": 3, "worse": 1, "matched": 2, "compared": 6,
                 "reason": "x"},
    }


def test_quorum_reached_would_adopt():
    probes = [_p(f"http://v{i}", "follower", "ADOPT") for i in range(5)] + [
        _p("http://v5", "follower", "REJECT"), _p("http://v6", "leader", "ADOPT"),
    ]
    _, verdicts = T.tally(probes, quorum=5)
    assert verdicts["cand_1"] is True  # 6 ADOPT >= 5


def test_quorum_not_reached_rejected():
    probes = [_p(f"http://v{i}", "follower", "REJECT") for i in range(5)] + [
        _p("http://v5", "follower", "ADOPT"), _p("http://v6", "leader", "ADOPT"),
    ]
    _, verdicts = T.tally(probes, quorum=5)
    assert verdicts["cand_1"] is False  # only 2 ADOPT < 5


def test_unreachable_and_missing_votes_are_noted():
    probes = [
        {"url": "http://down", "ok": False, "error": "timeout"},
        {"url": "http://novote", "ok": True, "vote": {}},
        _p("http://v1", "leader", "ADOPT"),
    ]
    lines, verdicts = T.tally(probes, quorum=1)
    assert any("UNREACHABLE" in ln for ln in lines)
    assert any("no independent_vote" in ln for ln in lines)
    assert verdicts["cand_1"] is True  # 1 ADOPT >= quorum 1


def test_fleet_from_leader_reads_quorum_and_peers(monkeypatch):
    payload = {
        "champion_consensus": {
            "quorum_required": 4,
            "peer_endpoints": [
                {"validator_id": "0xA", "url": "http://10.0.0.2:9100"},
                {"validator_id": "0xB", "url": "http://10.0.0.3:9100"},
            ],
        }
    }

    class _Resp:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

        def read(self_):
            return json.dumps(payload).encode()

    monkeypatch.setattr(T.urllib.request, "urlopen", lambda u, timeout=10: _Resp())
    fleet, quorum = T.fleet_from_leader("http://10.0.0.1:8080", 8080)
    assert quorum == 4
    assert fleet == ["http://10.0.0.1:8080", "http://10.0.0.2:8080", "http://10.0.0.3:8080"]
