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
