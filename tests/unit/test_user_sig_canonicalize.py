"""Unit tests for #229: relayer user-signature v-normalization."""

from minotaur_subnet.relayer.evm_relayer import canonicalize_user_signature

R = b"\x11" * 32
S = b"\x22" * 32


def test_v0_normalized_to_27():
    sig = R + S + bytes([0])
    out = canonicalize_user_signature(sig)
    assert out[:64] == R + S
    assert out[64] == 27


def test_v1_normalized_to_28():
    out = canonicalize_user_signature(R + S + bytes([1]))
    assert out[64] == 28


def test_v27_unchanged():
    sig = R + S + bytes([27])
    assert canonicalize_user_signature(sig) == sig


def test_v28_unchanged():
    sig = R + S + bytes([28])
    assert canonicalize_user_signature(sig) == sig


def test_empty_sig_unchanged():
    # Empty = multi-leg "skip user sig check" sentinel — must not be touched.
    assert canonicalize_user_signature(b"") == b""


def test_non_65_byte_sig_unchanged():
    # Anything not exactly 65 bytes is left as-is (e.g. a 64-byte compact sig).
    s = R + S
    assert canonicalize_user_signature(s) == s


def test_only_last_byte_changes():
    sig = R + S + bytes([0])
    out = canonicalize_user_signature(sig)
    assert out[:64] == sig[:64]  # r,s preserved exactly
    assert len(out) == 65
