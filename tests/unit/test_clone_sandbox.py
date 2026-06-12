"""Tests for the ephemeral-sandbox clone path in the screening pipeline.

Covers the pure logic — private-credential scoping, safe tar extraction, and
the http(s)-vs-file:// dispatch. The actual `docker run` is exercised by an
on-box integration check, not here.
"""

from __future__ import annotations

import base64
import io
import tarfile

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


class TestCloneDispatch:
    @pytest.mark.asyncio
    async def test_https_uses_sandbox(self, monkeypatch):
        called = {}

        async def fake_sb(u, c, d):
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

        async def fake_sb(u, c, d):
            called["sandbox"] = True
            return True

        async def fake_ip(u, c, d):
            called["in_process"] = True
            return True

        monkeypatch.setattr(sp, "_clone_repo_sandboxed", fake_sb)
        monkeypatch.setattr(sp, "_clone_repo_in_process", fake_ip)
        assert await sp._clone_repo("file:///repo", "abc1234", "/tmp/d") is True
        assert "in_process" in called and "sandbox" not in called
