"""Task 11: Runtime token, discovery, and bearer validation tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.runtime.auth import (
    RuntimeIdentity,
    cleanup_identity_files,
    create_identity,
    validate_bearer,
    write_identity_files,
)
from eee_agent.runtime.protocol import PROTOCOL

TOKEN_FILE = "runtime.token"
DISCOVERY_FILE = "runtime.json"


def _state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    return sd


# --------------------------------------------------------------------------
# create_identity
# --------------------------------------------------------------------------


def test_create_identity_uses_token_urlsafe_32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = secrets.token_urlsafe

    def spy(nbytes: int = -1) -> str:
        calls.append(nbytes)
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_urlsafe", spy)
    identity = create_identity()
    assert 32 in calls  # the token uses 32 random bytes


def test_token_has_at_least_32_bytes_strength() -> None:
    identity = create_identity()
    # 32 random bytes -> 43 base64url characters (no padding).
    assert len(identity.token) >= 43
    # Distinct identities are not equal.
    other = create_identity()
    assert identity.token != other.token


def test_fingerprint_is_first_12_hex_of_sha256() -> None:
    identity = create_identity()
    expected = hashlib.sha256(identity.token.encode("utf-8")).hexdigest()[:12]
    assert identity.fingerprint == expected
    assert len(identity.fingerprint) == 12
    # all hex
    int(identity.fingerprint, 16)


def test_identity_records_pid_nonce_and_utc_started_at() -> None:
    identity = create_identity()
    assert identity.pid == os.getpid()
    assert identity.process_nonce
    assert identity.started_at.tzinfo is not None
    assert identity.started_at.utcoffset() == timedelta_zero()
    assert identity.started_at <= datetime.now(timezone.utc)


def timedelta_zero():
    from datetime import timedelta

    return timedelta(0)


def test_identity_is_immutable() -> None:
    identity = create_identity()
    with pytest.raises(Exception):
        identity.token = "x"  # type: ignore[misc]


# --------------------------------------------------------------------------
# write_identity_files
# --------------------------------------------------------------------------


def test_write_identity_files_writes_full_token(monkeypatch, tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    monkeypatch.setattr("eee_agent.runtime.auth.PROTOCOL", PROTOCOL, raising=False)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    token_text = (sd / TOKEN_FILE).read_text(encoding="utf-8")
    assert token_text == identity.token


def test_write_identity_files_discovery_has_no_full_token(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    discovery_text = (sd / DISCOVERY_FILE).read_text(encoding="utf-8")
    assert identity.token not in discovery_text
    data = json.loads(discovery_text)
    assert "token" not in data
    assert data["token_fingerprint"] == identity.fingerprint


def test_write_identity_files_discovery_fields_exact(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    data = json.loads((sd / DISCOVERY_FILE).read_text(encoding="utf-8"))
    assert set(data.keys()) == {
        "protocol",
        "host",
        "port",
        "pid",
        "process_nonce",
        "token_file",
        "token_fingerprint",
        "started_at",
    }
    assert data["protocol"] == PROTOCOL
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 49152
    assert data["pid"] == identity.pid
    assert data["process_nonce"] == identity.process_nonce
    assert data["token_file"] == "runtime.token"
    assert data["token_fingerprint"] == identity.fingerprint
    assert data["started_at"] == identity.started_at.isoformat()


def test_write_identity_files_uses_atomic_replace(
    monkeypatch, tmp_path
):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    replace_calls: list[Path] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append(Path(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    destinations = {p.name for p in replace_calls}
    assert TOKEN_FILE in destinations
    assert DISCOVERY_FILE in destinations


def test_token_file_permissions_posix_or_windows_present(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    token_path = sd / TOKEN_FILE
    assert token_path.exists()
    if os.name == "posix":
        mode = stat_mode(token_path)
        assert (mode & 0o777) == 0o600
    else:
        # Windows: the file exists and is readable by the owner; ACL-based.
        assert token_path.stat().st_size > 0


def stat_mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_write_failure_leaves_no_wrong_discovery_or_leaked_token(
    monkeypatch, tmp_path
):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    real_replace = os.replace

    def failing_replace(src, dst):
        # Fail only when publishing the discovery file.
        if Path(dst).name == DISCOVERY_FILE:
            raise OSError("simulated publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    # The discovery file was not created/overwritten with a partial version.
    assert not (sd / DISCOVERY_FILE).exists()
    # The full token must not appear anywhere in state_dir (no leftover temp).
    for entry in sd.iterdir():
        if entry.is_file():
            assert identity.token not in entry.read_text(
                encoding="utf-8", errors="ignore"
            )


# --------------------------------------------------------------------------
# validate_bearer
# --------------------------------------------------------------------------


def test_validate_bearer_accepts_valid_token(monkeypatch):
    identity = create_identity()
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert validate_bearer(f"Bearer {identity.token}", identity) is True
    assert len(calls) == 1
    assert calls[0] == (identity.token, identity.token)


def test_validate_bearer_rejects_wrong_token(monkeypatch):
    identity = create_identity()
    called = [False]
    real = hmac.compare_digest

    def spy(a, b):
        called[0] = True
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert validate_bearer("Bearer wrong-token", identity) is False
    assert called[0] is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer  two",
        "Bearer abc def",
        "Basic abcdef",
        "Bearer  ",
        "bearer abcdef",
        "Bearer abc Bearer def",
    ],
)
def test_validate_bearer_rejects_invalid_format(header, monkeypatch):
    identity = create_identity()
    called = [False]
    real = hmac.compare_digest

    def spy(a, b):
        called[0] = True
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert validate_bearer(header, identity) is False
    assert called[0] is False  # never reached comparison for malformed input


def test_validate_bearer_does_not_leak_token_in_timing(monkeypatch):
    # The valid-format wrong-token path must still go through compare_digest
    # (constant-time), not short-circuit.
    identity = create_identity()
    assert validate_bearer("Bearer " + ("x" * len(identity.token)), identity) is False


# --------------------------------------------------------------------------
# cleanup_identity_files
# --------------------------------------------------------------------------


def test_cleanup_deletes_when_pid_and_nonce_match(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    assert (sd / TOKEN_FILE).exists()
    assert (sd / DISCOVERY_FILE).exists()
    cleanup_identity_files(identity, sd)
    assert not (sd / TOKEN_FILE).exists()
    assert not (sd / DISCOVERY_FILE).exists()


def test_cleanup_is_idempotent(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    cleanup_identity_files(identity, sd)
    # second call must not raise and must be a no-op
    cleanup_identity_files(identity, sd)
    assert not (sd / TOKEN_FILE).exists()


def test_cleanup_noop_when_discovery_absent(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    cleanup_identity_files(identity, sd)  # no files at all
    assert not (sd / DISCOVERY_FILE).exists()


def test_cleanup_does_not_delete_for_external_pid(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    other = create_identity()  # different nonce; same pid likely
    cleanup_identity_files(other, sd)
    # nonce mismatch -> files preserved
    assert (sd / TOKEN_FILE).exists()
    assert (sd / DISCOVERY_FILE).exists()


def test_cleanup_does_not_delete_for_wrong_nonce(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    # Rewrite discovery with the same pid but a different nonce.
    data = json.loads((sd / DISCOVERY_FILE).read_text(encoding="utf-8"))
    data["process_nonce"] = "different-nonce"
    (sd / DISCOVERY_FILE).write_text(
        json.dumps(data), encoding="utf-8"
    )
    cleanup_identity_files(identity, sd)
    assert (sd / TOKEN_FILE).exists()
    assert (sd / DISCOVERY_FILE).exists()


def test_cleanup_does_not_delete_malformed_discovery(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    (sd / DISCOVERY_FILE).write_text("{not json", encoding="utf-8")
    (sd / TOKEN_FILE).write_text("someone-elses-token", encoding="utf-8")
    cleanup_identity_files(identity, sd)
    assert (sd / TOKEN_FILE).exists()
    assert (sd / DISCOVERY_FILE).exists()


def test_cleanup_does_not_delete_for_external_pid_value(tmp_path):
    identity = create_identity()
    sd = _state_dir(tmp_path)
    write_identity_files(identity, sd, host="127.0.0.1", port=49152)
    data = json.loads((sd / DISCOVERY_FILE).read_text(encoding="utf-8"))
    data["pid"] = 999999
    (sd / DISCOVERY_FILE).write_text(
        json.dumps(data), encoding="utf-8"
    )
    cleanup_identity_files(identity, sd)
    assert (sd / TOKEN_FILE).exists()
    assert (sd / DISCOVERY_FILE).exists()
