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
_TOKEN_MODE = 0o600

# The full Bridge token lives ONLY in ``bridge.token``; discovery remains
# fingerprint-only. The two filenames are deliberately distinct from the Runtime
# handoff (``runtime.token`` / ``runtime.json``) so the credentials never cross.
BRIDGE_TOKEN_FILENAME = "bridge.token"
BRIDGE_DISCOVERY_FILENAME = "bridge.discovery.json"


class BridgeTokenError(Exception):
    """A bridge identity file is missing, empty, or malformed.

    The message never contains the token itself.
    """


class BridgeIdentityError(Exception):
    """A bridge identity could not be verified (e.g. fingerprint mismatch).

    The message never contains the token itself.
    """


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


def _atomic_write(path: Path, data: bytes, *, mode: int = _DISCOVERY_MODE) -> None:
    """Write ``data`` to ``path`` atomically via a same-dir temp + os.replace.

    Creates missing parent directories. On failure the temp file is removed and
    the final file is never partially written, so no token-bearing data leaks
    to an orphaned file. POSIX permissions (``mode``) are set on the temp file
    before the replace.
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
            os.chmod(tmp_path, mode)
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


# --------------------------------------------------------------------------
# Task 15-D: bridge.token handoff (full token via a separate same-dir file)
# --------------------------------------------------------------------------


def _fingerprint_for(token: str) -> str:
    """First :data:`_FINGERPRINT_HEX_CHARS` hex chars of the token's SHA-256."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_FINGERPRINT_HEX_CHARS]


def write_bridge_token(path: Path | str, identity: BridgeIdentity) -> None:
    """Atomically publish the full Bridge token to ``bridge.token``.

    The file contains exactly the UTF-8 token text plus one final newline — no
    JSON, metadata, or diagnostic text. On POSIX the file is mode ``0600``
    (owner-only); on Windows it inherits the per-user state-directory ACL. A
    failed write leaves no final file (atomic temp + ``os.replace``), and any
    stale file at ``path`` is replaced.
    """
    data = identity.token.encode("utf-8") + b"\n"
    _atomic_write(Path(path), data, mode=_TOKEN_MODE)


def read_bridge_token(path: Path | str) -> str:
    """Read and validate the Bridge token file at ``path``.

    Returns the full token. Raises :class:`BridgeTokenError` (whose message
    never contains the token) if the file is missing, empty, malformed (no
    single trailing newline / extra content), or not valid UTF-8.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError as exc:
        raise BridgeTokenError("bridge token file is missing") from exc
    except OSError as exc:
        raise BridgeTokenError("bridge token file is unreadable") from exc
    if not data:
        raise BridgeTokenError("bridge token file is empty")
    if not data.endswith(b"\n"):
        raise BridgeTokenError("bridge token file is malformed")
    body = data[:-1]
    try:
        token = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeTokenError("bridge token file is not valid UTF-8") from exc
    if not token or "\n" in token:
        raise BridgeTokenError("bridge token file is malformed")
    return token


def read_bridge_discovery(path: Path | str) -> dict[str, object]:
    """Read and parse the Bridge discovery file at ``path``.

    Raises :class:`BridgeTokenError` (no token in the message) if the file is
    missing, unreadable, or not strict JSON. The discovery payload carries only
    the fingerprint — never the full token.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BridgeTokenError("bridge discovery file is missing") from exc
    except OSError as exc:
        raise BridgeTokenError("bridge discovery file is unreadable") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise BridgeTokenError("bridge discovery file is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BridgeTokenError("bridge discovery file is not a JSON object")
    return payload


def write_bridge_identity_files(
    identity: BridgeIdentity,
    state_dir: Path | str,
    *,
    host: str,
    port: int,
) -> None:
    """Publish the token file then the discovery file (all-or-nothing).

    The token file is written first; discovery follows only after the token
    succeeds. If either atomic publication fails, both files are removed so no
    partial identity (token without discovery, or a half-written file) is left
    behind and no listener can be mistaken for a usable bridge. The original
    exception propagates.
    """
    state = Path(state_dir)
    token_path = state / BRIDGE_TOKEN_FILENAME
    discovery_path = state / BRIDGE_DISCOVERY_FILENAME
    try:
        write_bridge_token(token_path, identity)
        write_bridge_discovery(discovery_path, identity, host=host, port=port)
    except BaseException:
        for p in (token_path, discovery_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise


def remove_bridge_identity_files(state_dir: Path | str) -> None:
    """Remove the bridge token and discovery files from ``state_dir``.

    Idempotent: missing files are a no-op. Only the two identity files are
    touched; any other file in the directory is left alone.
    """
    state = Path(state_dir)
    for name in (BRIDGE_TOKEN_FILENAME, BRIDGE_DISCOVERY_FILENAME):
        try:
            (state / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def load_bridge_identity(state_dir: Path | str) -> BridgeIdentity:
    """Construct a Bridge identity from the discovery + ``bridge.token`` files.

    Reads the discovery (fingerprint only) and the full token, then verifies
    that the token's computed fingerprint matches the discovery fingerprint.
    Raises :class:`BridgeIdentityError` on a mismatch (no token in the message)
    and :class:`BridgeTokenError` on a missing/malformed file. The token never
    comes from an env var, CLI arg, Runtime token, or SQLite row.
    """
    state = Path(state_dir)
    discovery = read_bridge_discovery(state / BRIDGE_DISCOVERY_FILENAME)
    advertised = discovery.get("token_fingerprint")
    if not isinstance(advertised, str) or not advertised:
        raise BridgeIdentityError("bridge discovery has no token fingerprint")
    nonce = discovery.get("process_nonce")
    if not isinstance(nonce, str) or not nonce:
        raise BridgeIdentityError("bridge discovery has no process nonce")
    token = read_bridge_token(state / BRIDGE_TOKEN_FILENAME)
    actual = _fingerprint_for(token)
    if not hmac.compare_digest(actual, advertised):
        raise BridgeIdentityError("bridge token fingerprint does not match discovery")
    return BridgeIdentity(token=token, fingerprint=actual, process_nonce=nonce)
