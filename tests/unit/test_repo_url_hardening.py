"""Tests for repo_url policy hardening (rejects @-userinfo, IP literals)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from minotaur_subnet.api.routes.submissions.routes import (
    _validate_repo_url_policy,
    _looks_like_ip_literal,
)


# ── Helper ─────────────────────────────────────────────────────────────────

def test_ip_literal_helper():
    assert _looks_like_ip_literal("192.168.1.1") is True
    assert _looks_like_ip_literal("10.0.0.1") is True
    assert _looks_like_ip_literal("::1") is True
    assert _looks_like_ip_literal("2001:db8::1") is True
    assert _looks_like_ip_literal("github.com") is False
    assert _looks_like_ip_literal("gitlab.example.com") is False


# ── Happy path ─────────────────────────────────────────────────────────────

def test_https_github_com_passes(monkeypatch):
    monkeypatch.delenv("SUBMISSION_ALLOWED_REPO_HOSTS", raising=False)
    _validate_repo_url_policy("https://github.com/user/repo.git")


def test_http_rejected_without_opt_in(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_REPO_URLS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("http://github.com/user/repo.git")
    assert "HTTPS" in exc.value.detail


def test_http_allowed_with_opt_in(monkeypatch):
    monkeypatch.delenv("SUBMISSION_ALLOWED_REPO_HOSTS", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_REPO_URLS", "1")
    _validate_repo_url_policy("http://gitea.local/user/repo.git")


# ── Hardening: @-userinfo shadowing ───────────────────────────────────────

def test_at_userinfo_rejected():
    """https://github.com@attacker.com/x would clone from attacker.com."""
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("https://github.com@attacker.com/x.git")
    assert "userinfo" in exc.value.detail.lower()


def test_at_userinfo_even_with_credentials_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("https://user:pass@attacker.com/x.git")
    assert "userinfo" in exc.value.detail.lower()


# ── Hardening: IP literals ─────────────────────────────────────────────────

def test_ipv4_literal_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("https://192.168.1.1/x.git")
    assert "DNS" in exc.value.detail


def test_ipv6_literal_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("https://[2001:db8::1]/x.git")
    assert "DNS" in exc.value.detail


# ── Allowlist enforcement ──────────────────────────────────────────────────

def test_allowlist_enforced(monkeypatch):
    monkeypatch.setenv("SUBMISSION_ALLOWED_REPO_HOSTS", "github.com,gitlab.com")
    _validate_repo_url_policy("https://github.com/user/repo.git")
    _validate_repo_url_policy("https://gitlab.com/user/repo.git")

    with pytest.raises(HTTPException) as exc:
        _validate_repo_url_policy("https://bitbucket.org/user/repo.git")
    assert "not allowed" in exc.value.detail


def test_allowlist_case_insensitive(monkeypatch):
    monkeypatch.setenv("SUBMISSION_ALLOWED_REPO_HOSTS", "GitHub.com")
    _validate_repo_url_policy("https://github.com/user/repo.git")
    _validate_repo_url_policy("https://GITHUB.COM/user/repo.git")


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_bad_scheme_rejected():
    with pytest.raises(HTTPException):
        _validate_repo_url_policy("ftp://github.com/x.git")


def test_empty_host_rejected():
    with pytest.raises(HTTPException):
        _validate_repo_url_policy("https:///x.git")


def test_subdomain_must_match_exactly(monkeypatch):
    """attacker.github.com must NOT match an allowlist of 'github.com'."""
    monkeypatch.setenv("SUBMISSION_ALLOWED_REPO_HOSTS", "github.com")
    with pytest.raises(HTTPException):
        _validate_repo_url_policy("https://attacker.github.com/x.git")


def test_domain_containing_allowed_as_suffix_rejected(monkeypatch):
    """xgithub.com must NOT match an allowlist of 'github.com'."""
    monkeypatch.setenv("SUBMISSION_ALLOWED_REPO_HOSTS", "github.com")
    with pytest.raises(HTTPException):
        _validate_repo_url_policy("https://xgithub.com/x.git")
