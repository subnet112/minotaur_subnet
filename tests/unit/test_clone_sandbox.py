"""Tests for the ephemeral-sandbox clone path in the screening pipeline.

Covers the pure logic — private-credential scoping, safe tar extraction, and
the http(s)-vs-file:// dispatch. The actual `docker run` is exercised by an
on-box integration check, not here.
"""

from __future__ import annotations

import base64
import io
import tarfile
from unittest.mock import AsyncMock

import pytest

from minotaur_subnet.api.routes.submissions import screening_pipeline as sp


def _make_tar(files: dict[str, bytes], links: list[tuple[str, str]] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
        for name, linkname in links or []:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = linkname
            tf.addfile(ti)
    return buf.getvalue()


class TestResolveCloneBasicAuth:
    def test_no_creds(self, monkeypatch):
        for k in (
            "SUBMISSION_GIT_CLONE_USERNAME",
            "SUBMISSION_GIT_CLONE_PASSWORD",
            "SUBMISSION_GIT_CLONE_ALLOWED_HOSTS",
        ):
            monkeypatch.delenv(k, raising=False)
        assert sp._resolve_clone_basic_auth("https://github.com/x/y") is None

    def test_partial_creds(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_USERNAME", "u")
        monkeypatch.delenv("SUBMISSION_GIT_CLONE_PASSWORD", raising=False)
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", "github.com")
        assert sp._resolve_clone_basic_auth("https://github.com/x/y") is None

    def test_no_allowlist_refuses(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_USERNAME", "u")
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_PASSWORD", "p")
        monkeypatch.delenv("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", raising=False)
        assert sp._resolve_clone_basic_auth("https://github.com/x/y") is None

    def test_host_not_allowed(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_USERNAME", "u")
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_PASSWORD", "p")
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", "example.com")
        assert sp._resolve_clone_basic_auth("https://github.com/x/y") is None

    def test_allowed_host(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_USERNAME", "u")
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_PASSWORD", "p")
        monkeypatch.setenv("SUBMISSION_GIT_CLONE_ALLOWED_HOSTS", "github.com,gitlab.com")
        assert sp._resolve_clone_basic_auth("https://github.com/x/y") == (
            base64.b64encode(b"u:p").decode()
        )


class TestSafeExtractTar:
    def test_valid(self, tmp_path):
        data = _make_tar({"solver.py": b"print(1)", "sub/x.txt": b"hi"})
        assert sp._safe_extract_tar(data, str(tmp_path)) is True
        assert (tmp_path / "solver.py").read_bytes() == b"print(1)"
        assert (tmp_path / "sub" / "x.txt").read_bytes() == b"hi"

    def test_empty_rejected(self, tmp_path):
        assert sp._safe_extract_tar(b"", str(tmp_path)) is False

    def test_path_traversal_rejected(self, tmp_path):
        dest = tmp_path / "dest"
        data = _make_tar({"../escape.txt": b"bad"})
        assert sp._safe_extract_tar(data, str(dest)) is False
        assert not (tmp_path / "escape.txt").exists()

    def test_absolute_symlink_rejected(self, tmp_path):
        data = _make_tar({}, links=[("evil", "/etc/passwd")])
        assert sp._safe_extract_tar(data, str(tmp_path)) is False

    def test_oversize_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "MAX_CLONE_TAR_BYTES", 16)
        data = _make_tar({"big": b"x" * 1024})
        assert sp._safe_extract_tar(data, str(tmp_path)) is False

    def test_member_count_cap_rejects_inode_bomb(self, tmp_path, monkeypatch):
        # Many ZERO-byte files pass the byte cap (total size ~0) but would exhaust
        # inodes on extract — the member-count cap must reject them.
        monkeypatch.setattr(sp, "MAX_CLONE_TAR_MEMBERS", 10)
        data = _make_tar({f"f{i}": b"" for i in range(25)})
        assert sp._safe_extract_tar(data, str(tmp_path)) is False
        # a normal small tree is unaffected
        assert sp._safe_extract_tar(_make_tar({"solver.py": b"x"}), str(tmp_path)) is True


class TestCloneDispatch:
    @pytest.mark.asyncio
    async def test_https_uses_sandbox(self, monkeypatch):
        called = {}

        async def fake_sb(u, c, d, token=None):
            called["sandbox"] = (u, c, d)
            return True

        async def fake_ip(u, c, d):
            called["in_process"] = True
            return True

        monkeypatch.setattr(sp, "_clone_repo_sandboxed", fake_sb)
        monkeypatch.setattr(sp, "_clone_repo_in_process", fake_ip)
        assert await sp._clone_repo("https://github.com/x/y", "abc1234", "/tmp/d") is True
        assert "sandbox" in called and "in_process" not in called

    @pytest.mark.asyncio
    async def test_file_uses_in_process(self, monkeypatch):
        called = {}

        async def fake_sb(u, c, d, token=None):
            called["sandbox"] = True
            return True

        async def fake_ip(u, c, d):
            called["in_process"] = True
            return True

        monkeypatch.setattr(sp, "_clone_repo_sandboxed", fake_sb)
        monkeypatch.setattr(sp, "_clone_repo_in_process", fake_ip)
        assert await sp._clone_repo("file:///repo", "abc1234", "/tmp/d") is True
        assert "in_process" in called and "sandbox" not in called


class TestCloneRetry:
    """The sandboxed clone retries a transient failure (a truncated tarball →
    'unexpected end of data') so a one-off network blip doesn't reject an
    otherwise-valid submission."""

    def test_clone_attempts_default_is_three(self, monkeypatch):
        monkeypatch.delenv("SUBMISSION_CLONE_RETRIES", raising=False)
        assert sp._clone_attempts() == 3  # 1 + 2 retries

    def test_clone_attempts_env_override(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_CLONE_RETRIES", "4")
        assert sp._clone_attempts() == 5

    def test_clone_attempts_zero_disables_retries(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_CLONE_RETRIES", "0")
        assert sp._clone_attempts() == 1

    def test_clone_attempts_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("SUBMISSION_CLONE_RETRIES", "garbage")
        assert sp._clone_attempts() == 3

    def test_clear_dir_empties_but_keeps_dir(self, tmp_path):
        (tmp_path / "f.txt").write_text("x")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "g.txt").write_text("y")
        sp._clear_dir(str(tmp_path))
        assert tmp_path.exists()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_and_clears_between(self, monkeypatch, tmp_path):
        calls = []

        async def fake_sandbox(repo_url, commit, dest, *, token=None):
            calls.append(1)
            return len(calls) >= 3  # fail attempts 1 & 2, succeed on 3

        cleared = []
        monkeypatch.setattr(sp, "_clone_repo_sandboxed", fake_sandbox)
        monkeypatch.setattr(sp, "_clear_dir", lambda p: cleared.append(p))
        monkeypatch.setattr(sp.asyncio, "sleep", AsyncMock())
        monkeypatch.delenv("SUBMISSION_CLONE_RETRIES", raising=False)

        ok = await sp._clone_repo("https://github.com/x/y", "abc", str(tmp_path))

        assert ok is True
        assert len(calls) == 3          # retried until success
        assert cleared == [str(tmp_path)] * 2  # dest cleared before attempts 2 and 3

    @pytest.mark.asyncio
    async def test_all_attempts_fail_returns_false(self, monkeypatch, tmp_path):
        n = []

        async def always_fail(*a, **k):
            n.append(1)
            return False

        monkeypatch.setattr(sp, "_clone_repo_sandboxed", always_fail)
        monkeypatch.setattr(sp.asyncio, "sleep", AsyncMock())
        monkeypatch.setenv("SUBMISSION_CLONE_RETRIES", "2")

        ok = await sp._clone_repo("https://github.com/x/y", "abc", str(tmp_path))

        assert ok is False
        assert len(n) == 3  # 1 + 2 retries, all attempted

    @pytest.mark.asyncio
    async def test_first_attempt_success_no_retry_no_sleep(self, monkeypatch, tmp_path):
        n = []
        cleared = []

        async def ok_first(*a, **k):
            n.append(1)
            return True

        monkeypatch.setattr(sp, "_clone_repo_sandboxed", ok_first)
        monkeypatch.setattr(sp, "_clear_dir", lambda p: cleared.append(p))
        sleep = AsyncMock()
        monkeypatch.setattr(sp.asyncio, "sleep", sleep)

        ok = await sp._clone_repo("https://github.com/x/y", "abc", str(tmp_path))

        assert ok is True
        assert n == [1]        # no retry
        assert cleared == []   # no clear on the first attempt
        sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_scheme_uses_in_process_and_does_not_retry(self, monkeypatch, tmp_path):
        sandbox = AsyncMock()
        inproc = AsyncMock(return_value=True)
        monkeypatch.setattr(sp, "_clone_repo_sandboxed", sandbox)
        monkeypatch.setattr(sp, "_clone_repo_in_process", inproc)

        ok = await sp._clone_repo("file:///repo", "abc", str(tmp_path))

        assert ok is True
        sandbox.assert_not_called()      # file:// never uses the sandboxed/retry path
        inproc.assert_awaited_once()
