"""Unit tests for content-addressed image transport helpers (P1)."""

from minotaur_subnet.harness import image_transport as it

REPO = "ghcr.io/subnet112/minotaur-solver-candidates"
HEX = "a" * 64
REF = f"{REPO}@sha256:{HEX}"


def test_leader_pushes_digests_keys_off_candidate_repo(monkeypatch):
    # Leader-local capability gate — NOT a per-validator consensus mode env.
    monkeypatch.delenv("CANDIDATE_IMAGE_REPO", raising=False)
    assert it.leader_pushes_digests() is False          # unset -> leader does not push
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", "ghcr.io/x/y")
    assert it.leader_pushes_digests() is True           # explicit opt-in


def test_candidate_repo_default_and_override(monkeypatch):
    monkeypatch.delenv("CANDIDATE_IMAGE_REPO", raising=False)
    assert it.candidate_repo() == it.DEFAULT_CANDIDATE_REPO
    monkeypatch.setenv("CANDIDATE_IMAGE_REPO", "ghcr.io/x/y")
    assert it.candidate_repo() == "ghcr.io/x/y"


def test_bare_hex_from_all_shapes():
    assert it.bare_hex(REF) == HEX                 # <repo>@sha256:<hex>
    assert it.bare_hex(f"sha256:{HEX}") == HEX     # docker {{.Id}} shape
    assert it.bare_hex(HEX) == HEX                 # already bare
    assert it.bare_hex(HEX.upper()) == HEX         # normalized lowercase
    assert it.bare_hex(None) is None
    assert it.bare_hex("") is None
    assert it.bare_hex("not-a-digest") is None
    assert it.bare_hex("sha256:short") is None
    assert it.bare_hex("a" * 63) is None           # wrong length rejected


def test_is_digest_ref():
    assert it.is_digest_ref(REF) is True
    assert it.is_digest_ref(f"sha256:{HEX}") is False   # no repo -> not pullable
    assert it.is_digest_ref(HEX) is False
    assert it.is_digest_ref(None) is False
    assert it.is_digest_ref(f"{REPO}@sha256:short") is False


def test_parse_repo_digest():
    assert it.parse_repo_digest(REF) == (REPO, HEX)
    assert it.parse_repo_digest(f"sha256:{HEX}") is None
    assert it.parse_repo_digest(None) is None


def test_make_digest_ref():
    assert it.make_digest_ref(REPO, HEX) == REF
    assert it.make_digest_ref(REPO, f"sha256:{HEX}") == REF
    assert it.make_digest_ref(REPO, REF) == REF    # idempotent from a full ref
    assert it.make_digest_ref(REPO, "bad") is None
    assert it.make_digest_ref("", HEX) is None
