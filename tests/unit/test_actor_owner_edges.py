"""SubmissionStore.actor_owner_edges feeds the actor-key owner union."""
from minotaur_subnet.harness.submission_store import SubmissionStore


def _sub(store, hotkey, owner):
    return store.create(
        repo_url="https://example.com/r.git", commit_hash="c"*40, epoch=1,
        hotkey=hotkey, round_id="r1", max_per_round=0, max_rounds_per_commit=0,
        github_owner=owner,
    )


def test_owner_edges_dedup_and_multi_owner():
    s = SubmissionStore(persist_path=None)
    _sub(s, "hkA", "op1")
    _sub(s, "hkA", "op1")   # dup owner -> deduped
    _sub(s, "hkA", "op2")   # hkA used two owners
    _sub(s, "hkB", "op2")
    edges = s.actor_owner_edges()
    assert set(edges["hkA"]) == {"op1", "op2"}
    assert edges["hkB"] == ["op2"]


def test_owner_edges_skips_missing_owner():
    s = SubmissionStore(persist_path=None)
    s.create(repo_url="https://e/r.git", commit_hash="c"*40, epoch=1,
             hotkey="hkC", round_id="r1", max_per_round=0, max_rounds_per_commit=0)
    assert "hkC" not in s.actor_owner_edges()
