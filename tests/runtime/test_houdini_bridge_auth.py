"""Task 15-B: Bridge identity, token validation, and discovery tests.

Pure-Python tests for the Bridge-side identity that is fully independent of the
Runtime identity. The full Bridge token lives only in memory (and the hello
frame); discovery carries a SHA-256 fingerprint only. No ``hou``/``rpyc``.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

import pytest

from eee_agent.houdini_bridge.auth import (
    BridgeIdentity,
    create_bridge_identity,
    discovery_payload,
    validate_bridge_token,
    write_bridge_discovery,
)

PROTO = "eee.bridge/1"


# --------------------------------------------------------------------------
# create_bridge_identity
# --------------------------------------------------------------------------


def test_token_uses_at_least_32_random_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = secrets.token_urlsafe

    def spy(nbytes: int = -1) -> str:
        calls.append(nbytes)
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_urlsafe", spy)
    create_bridge_identity()
    assert 32 in calls  # the bearer token uses 32 random bytes


def test_token_has_at_least_32_bytes_strength() -> None:
    identity = create_bridge_identity()
    # 32 random bytes -> >= 43 base64url characters (no padding).
    assert len(identity.token) >= 43


def test_two_identities_are_distinct() -> None:
    a = create_bridge_identity()
    b = create_bridge_identity()
    assert a.token != b.token
    assert a.fingerprint != b.fingerprint
    assert a.process_nonce != b.process_nonce


def test_fingerprint_is_sha256_based() -> None:
    identity = create_bridge_identity()
    digest = hashlib.sha256(identity.token.encode("utf-8")).hexdigest()
    assert digest.startswith(identity.fingerprint)
    assert len(identity.fingerprint) >= 12  # at least 12 hex characters
    int(identity.fingerprint, 16)  # all hex digits


def test_identity_is_immutable() -> None:
    identity = create_bridge_identity()
    with pytest.raises(Exception):
        identity.token = "x"  # type: ignore[misc]


# --------------------------------------------------------------------------
# validate_bridge_token
# --------------------------------------------------------------------------


def test_validate_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import hmac

    identity = create_bridge_identity()
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert validate_bridge_token(identity, identity.token) is True
    assert len(calls) == 1
    assert calls[0] == (identity.token, identity.token)


def test_validate_rejects_wrong_token() -> None:
    identity = create_bridge_identity()
    assert validate_bridge_token(identity, "totally-wrong-token") is False
    assert validate_bridge_token(identity, "") is False
    assert validate_bridge_token(identity, "x" * len(identity.token)) is False


@pytest.mark.parametrize(
    "presented",
    [None, 123, 1.5, b"bytes-are-not-a-string", True, object()],
    ids=["none", "int", "float", "bytes", "bool", "object"],
)
def test_validate_rejects_non_string_token(presented: object) -> None:
    identity = create_bridge_identity()
    assert validate_bridge_token(identity, presented) is False


# --------------------------------------------------------------------------
# discovery_payload
# --------------------------------------------------------------------------


def test_discovery_has_no_full_token() -> None:
    identity = create_bridge_identity()
    payload = discovery_payload(identity, host="127.0.0.1", port=49152)
    text = json.dumps(payload, ensure_ascii=False)
    assert identity.token not in text
    assert "token" not in payload
    assert "bearer" not in text.lower()


def test_discovery_contains_required_fields() -> None:
    identity = create_bridge_identity()
    payload = discovery_payload(identity, host="127.0.0.1", port=49152)
    assert set(payload.keys()) == {
        "protocol",
        "host",
        "port",
        "token_fingerprint",
        "process_nonce",
    }
    assert payload["protocol"] == PROTO
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 49152
    assert payload["token_fingerprint"] == identity.fingerprint
    assert payload["process_nonce"] == identity.process_nonce


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "localhost", "10.0.0.1", "::1", "", "127.0.0.2"]
)
def test_discovery_rejects_non_loopback_host(host: str) -> None:
    identity = create_bridge_identity()
    with pytest.raises((ValueError, TypeError)):
        discovery_payload(identity, host=host, port=49152)


@pytest.mark.parametrize(
    "port",
    [True, 1.0, 0, 65536, -1, "8080", None],
    ids=["bool", "float", "zero", "too-big", "negative", "str", "none"],
)
def test_discovery_rejects_bad_port(port: object) -> None:
    identity = create_bridge_identity()
    with pytest.raises((ValueError, TypeError)):
        discovery_payload(identity, host="127.0.0.1", port=port)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# write_bridge_discovery
# --------------------------------------------------------------------------


def test_write_discovery_uses_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = create_bridge_identity()
    destinations: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst):  # type: ignore[no-untyped-def]
        destinations.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    path = tmp_path / "bridge.json"
    write_bridge_discovery(path, identity, host="127.0.0.1", port=49152)
    assert "bridge.json" in destinations
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["token_fingerprint"] == identity.fingerprint
    assert identity.token not in path.read_text(encoding="utf-8")


def test_write_discovery_creates_missing_parent_dirs(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / "nested" / "dir" / "bridge.json"
    write_bridge_discovery(path, identity, host="127.0.0.1", port=49152)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["protocol"] == PROTO
    assert identity.token not in path.read_text(encoding="utf-8")


def test_atomic_replace_failure_leaves_no_partial_final_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = create_bridge_identity()

    def failing_replace(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("simulated publish failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    path = tmp_path / "bridge.json"
    with pytest.raises(OSError):
        write_bridge_discovery(path, identity, host="127.0.0.1", port=49152)
    # The final discovery file was never created.
    assert not path.exists()
    # No leftover temp file carries the token (or the fingerprint).
    for entry in tmp_path.iterdir():
        if entry.is_file():
            text = entry.read_text(encoding="utf-8", errors="ignore")
            assert identity.token not in text


# --------------------------------------------------------------------------
# independence + token hygiene
# --------------------------------------------------------------------------


def test_bridge_token_is_independent_of_runtime_token() -> None:
    from eee_agent.runtime.auth import (
        create_identity as create_runtime_identity,
        validate_bearer,
    )

    bridge = create_bridge_identity()
    runtime = create_runtime_identity()
    assert bridge.token != runtime.token
    assert bridge.fingerprint != runtime.fingerprint
    # A Runtime token must not authenticate as a Bridge token, and vice versa.
    assert validate_bridge_token(bridge, runtime.token) is False
    assert validate_bearer(f"Bearer {bridge.token}", runtime) is False


def test_full_token_never_appears_in_repr_or_exceptions() -> None:
    identity = create_bridge_identity()
    assert identity.token not in repr(identity)
    rebuilt = BridgeIdentity(
        token=identity.token,
        fingerprint=identity.fingerprint,
        process_nonce=identity.process_nonce,
    )
    assert identity.token not in repr(rebuilt)
    # Exceptions raised by the discovery helpers must not embed the token.
    try:
        discovery_payload(identity, host="0.0.0.0", port=49152)
    except Exception as exc:  # noqa: BLE001 — we only inspect the message
        assert identity.token not in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected a ValueError for a non-loopback host")


# ==========================================================================
# Task 15-D: bridge.token handoff (token file separate from discovery)
#
# The full Bridge token travels ONLY through a same-directory ``bridge.token``
# file (UTF-8 token text + one final newline). Discovery remains
# fingerprint-only. Files are published atomically and cleaned up idempotently.
# No ``hou``/``rpyc``.
# ==========================================================================

import os
import stat

from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    BridgeIdentityError,
    BridgeTokenError,
    load_bridge_identity,
    read_bridge_discovery,
    read_bridge_token,
    remove_bridge_identity_files,
    write_bridge_identity_files,
    write_bridge_token,
)


# --- 1. atomic write -------------------------------------------------------


def test_write_bridge_token_uses_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = create_bridge_identity()
    destinations: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst):  # type: ignore[no-untyped-def]
        destinations.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    write_bridge_token(path, identity)
    assert BRIDGE_TOKEN_FILENAME in destinations
    assert path.read_text(encoding="utf-8") == identity.token + "\n"


# --- 2. exact file contents ------------------------------------------------


def test_bridge_token_file_is_token_plus_one_newline(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    write_bridge_token(path, identity)
    raw = path.read_bytes()
    assert raw == identity.token.encode("utf-8") + b"\n"
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1  # exactly one final newline
    # no JSON, no metadata, no extra payload
    for marker in ("{", "}", '"', "fingerprint", "token_fingerprint"):
        assert marker not in text


# --- 3. read restores the full token --------------------------------------


def test_read_bridge_token_restores_full_token(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    write_bridge_token(path, identity)
    assert read_bridge_token(path) == identity.token


# --- 4. missing / empty / malformed ---------------------------------------


def test_read_bridge_token_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(BridgeTokenError):
        read_bridge_token(tmp_path / BRIDGE_TOKEN_FILENAME)


def test_read_bridge_token_empty_raises(tmp_path: Path) -> None:
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    path.write_bytes(b"")
    with pytest.raises(BridgeTokenError):
        read_bridge_token(path)


def test_read_bridge_token_without_trailing_newline_raises(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    path.write_text(identity.token, encoding="utf-8")  # no trailing newline
    with pytest.raises(BridgeTokenError):
        read_bridge_token(path)


def test_read_bridge_token_with_extra_content_raises(tmp_path: Path) -> None:
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    path.write_bytes(b"sometoken\nEXTRA-GARBAGE\n")
    with pytest.raises(BridgeTokenError):
        read_bridge_token(path)


def test_read_bridge_token_error_carries_no_token(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    # Malformed on purpose: valid token but no trailing newline.
    path.write_text(identity.token, encoding="utf-8")
    with pytest.raises(BridgeTokenError) as exc:
        read_bridge_token(path)
    assert identity.token not in str(exc.value)
    assert identity.token not in repr(exc.value)


# --- 5. discovery JSON carries fingerprint only ---------------------------


def test_write_identity_files_discovery_has_no_token(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    discovery_path = tmp_path / BRIDGE_DISCOVERY_FILENAME
    text = discovery_path.read_text(encoding="utf-8")
    assert identity.token not in text
    data = json.loads(text)
    assert data["token_fingerprint"] == identity.fingerprint
    # No raw token key, no bearer field.
    assert "token" not in data
    assert "bearer" not in text.lower()


def test_read_bridge_discovery_returns_payload(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    discovery = read_bridge_discovery(tmp_path / BRIDGE_DISCOVERY_FILENAME)
    assert discovery["protocol"] == PROTO
    assert discovery["host"] == "127.0.0.1"
    assert discovery["port"] == 49152
    assert discovery["token_fingerprint"] == identity.fingerprint
    assert discovery["process_nonce"] == identity.process_nonce
    assert "token" not in discovery


def test_read_bridge_discovery_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(BridgeTokenError):
        read_bridge_discovery(tmp_path / BRIDGE_DISCOVERY_FILENAME)


# --- 6. token absent from repr / exceptions / diagnostics -----------------


def test_token_absent_from_all_diagnostics(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    assert identity.token not in repr(identity)
    # A malformed token file error must not embed the token.
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    path.write_bytes(b"")
    with pytest.raises(BridgeTokenError) as exc:
        read_bridge_token(path)
    assert identity.token not in str(exc.value)


# --- 7. stale token file replaced by a new identity -----------------------


def test_stale_token_file_replaced_by_new_identity(tmp_path: Path) -> None:
    old = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    write_bridge_token(path, old)
    assert read_bridge_token(path) == old.token
    new = create_bridge_identity()
    write_bridge_token(path, new)
    assert read_bridge_token(path) == new.token
    assert read_bridge_token(path) != old.token


# --- 8. cleanup is idempotent ---------------------------------------------


def test_remove_identity_files_is_idempotent(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    remove_bridge_identity_files(tmp_path)
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
    # Calling again on an empty directory must be a no-op (no raise).
    remove_bridge_identity_files(tmp_path)
    remove_bridge_identity_files(tmp_path)


def test_remove_identity_files_removes_only_identity_files(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    extra = tmp_path / "unrelated.txt"
    extra.write_text("keep me", encoding="utf-8")
    remove_bridge_identity_files(tmp_path)
    assert extra.exists()  # unrelated file untouched
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()


# --- 9. POSIX owner-only permissions / Windows local state ----------------


def test_bridge_token_file_protection(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    path = tmp_path / BRIDGE_TOKEN_FILENAME
    write_bridge_token(path, identity)
    assert path.exists()
    if os.name == "posix":
        # POSIX: the token file is owner-only (0600).
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
    else:
        # Windows: there is no portable UNIX mode bit. The contract is that the
        # token lives in the current user's machine-local state directory (here a
        # per-process tmp dir), never in a shared/public location.
        assert str(path).startswith(str(tmp_path))


# --- 10. publish failure leaves no token and no discovery ------------------


def test_token_publish_failure_leaves_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = create_bridge_identity()

    def failing_replace(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("simulated token publish failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
    # No leftover temp file carries the token.
    for entry in tmp_path.iterdir():
        if entry.is_file():
            assert identity.token not in entry.read_text(encoding="utf-8", errors="ignore")


def test_discovery_failure_after_token_rolls_back_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = create_bridge_identity()
    real_replace = os.replace
    count = {"n": 0}

    def spy(src, dst):  # type: ignore[no-untyped-def]
        count["n"] += 1
        # Token write is the first replace; discovery write is the second.
        if count["n"] >= 2:
            raise OSError("simulated discovery publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    with pytest.raises(OSError):
        write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    # The token file was rolled back; discovery was never finalized.
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()


# --- 11 / 12. load identity verifies fingerprint; mismatch rejected --------


def test_load_identity_round_trips_and_verifies_fingerprint(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    loaded = load_bridge_identity(tmp_path)
    assert loaded.token == identity.token
    assert loaded.fingerprint == identity.fingerprint
    assert loaded.process_nonce == identity.process_nonce


def test_load_identity_rejects_fingerprint_mismatch(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    # Replace the token file with a different token whose fingerprint differs.
    other = create_bridge_identity()
    (tmp_path / BRIDGE_TOKEN_FILENAME).write_bytes(other.token.encode("utf-8") + b"\n")
    with pytest.raises(BridgeIdentityError) as exc:
        load_bridge_identity(tmp_path)
    # Neither token may appear in the diagnostic.
    assert identity.token not in str(exc.value)
    assert other.token not in str(exc.value)


def test_load_identity_missing_token_rejected(tmp_path: Path) -> None:
    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    (tmp_path / BRIDGE_TOKEN_FILENAME).unlink()
    with pytest.raises(BridgeTokenError):
        load_bridge_identity(tmp_path)


# --- 13. Bridge token stays independent of the Runtime token --------------


def test_bridge_identity_files_are_distinct_from_runtime(tmp_path: Path) -> None:
    from eee_agent.runtime.auth import TOKEN_FILENAME as RUNTIME_TOKEN_FILENAME

    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=49152)
    assert BRIDGE_TOKEN_FILENAME != RUNTIME_TOKEN_FILENAME
    assert (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    # No runtime token file is created by the bridge handoff.
    assert not (tmp_path / RUNTIME_TOKEN_FILENAME).exists()


def test_bridge_token_does_not_authenticate_runtime(tmp_path: Path) -> None:
    from eee_agent.runtime.auth import create_identity as create_runtime_identity
    from eee_agent.runtime.auth import validate_bearer

    bridge = create_bridge_identity()
    runtime = create_runtime_identity()
    # A Bridge token must fail Runtime bearer validation, and vice versa.
    assert validate_bearer(f"Bearer {bridge.token}", runtime) is False
    assert validate_bridge_token(bridge, runtime.token) is False
    # The two identities never share a token or fingerprint.
    assert bridge.token != runtime.token
    assert bridge.fingerprint != runtime.fingerprint
