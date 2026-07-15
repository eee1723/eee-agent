"""Bridge identity: independent bearer token, fingerprint, and discovery.

The Bridge token is generated, stored, and rotated **independently** of the
Runtime token. The full token lives only in memory (and the hello frame); the
discovery payload carries a SHA-256 fingerprint and never the token itself.
Discovery is published via a same-directory temp file + ``os.replace`` so a
write failure cannot leave a partial final file.

This module imports neither ``hou`` nor ``rpyc`` and does not modify
``eee_agent.runtime.auth``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from eee_agent.houdini_bridge.contracts import PROTOCOL

_LOOPBACK_HOST = "127.0.0.1"
_MIN_PORT = 1
_MAX_PORT = 65535
_FINGERPRINT_HEX_CHARS = 16  # >= 12 hex characters of the SHA-256 digest
_DISCOVERY_MODE = 0o644


def _require_loopback(host: object) -> str:
    if type(host) is not str or host != _LOOPBACK_HOST:
        raise ValueError("Bridge host must be 127.0.0.1")
    return host


def _require_port(port: object) -> int:
    if type(port) is not int or port < _MIN_PORT or port > _MAX_PORT:
        raise ValueError("Bridge port must be an integer in 1..65535")
    return port


@dataclass(frozen=True, slots=True)
class BridgeIdentity:
    """A Bridge process's bearer token and identifying metadata.

    The token is excluded from ``repr`` so it never appears in logs or error
    text. ``fingerprint`` is a truncation of the token's SHA-256 digest; it is
    safe to publish. ``process_nonce`` is an independent random value.
    """

    token: str = field(repr=False)
    fingerprint: str
    process_nonce: str


def create_bridge_identity() -> BridgeIdentity:
    """Create a fresh, independent Bridge identity.

    The token uses ``secrets.token_urlsafe(32)`` (32 random bytes). The
    fingerprint is the first :data:`_FINGERPRINT_HEX_CHARS` hex characters of
    the token's SHA-256. The process nonce is an independent random value.
    """
    token = secrets.token_urlsafe(32)
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[
        :_FINGERPRINT_HEX_CHARS
    ]
    process_nonce = secrets.token_urlsafe(16)
    return BridgeIdentity(
        token=token, fingerprint=fingerprint, process_nonce=process_nonce
    )


def validate_bridge_token(identity: BridgeIdentity, presented: object) -> bool:
    """Validate ``presented`` against ``identity.token`` (constant-time).

    Non-string input returns ``False`` without comparison. A well-formed
    string is always compared with ``hmac.compare_digest`` so timing never
    leaks whether the token matched. This function never raises and never
    embeds the token in an exception.
    """
    if not isinstance(presented, str):
        return False
    return hmac.compare_digest(presented, identity.token)


def discovery_payload(
    identity: BridgeIdentity, *, host: str, port: int
) -> dict[str, object]:
    """Build the public Bridge discovery dict.

    Carries the protocol, loopback host/port, the token fingerprint, and the
    process nonce — never the full token.
    """
    _require_loopback(host)
    _require_port(port)
    return {
        "protocol": PROTOCOL,
        "host": host,
        "port": port,
        "token_fingerprint": identity.fingerprint,
        "process_nonce": identity.process_nonce,
    }


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via a same-dir temp + os.replace.

    Creates missing parent directories. On failure the temp file is removed and
    the final file is never partially written, so no token-bearing data leaks
    to an orphaned file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp_path, _DISCOVERY_MODE)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def write_bridge_discovery(
    path: Path | str,
    identity: BridgeIdentity,
    *,
    host: str,
    port: int,
) -> None:
    """Atomically publish the Bridge discovery file at ``path``.

    The discovery dict (fingerprint only — never the full token) is written via
    :func:`_atomic_write`. If the atomic replace fails, no partial final file
    is left behind.
    """
    payload = discovery_payload(identity, host=host, port=port)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    _atomic_write(Path(path), text.encode("utf-8"))
