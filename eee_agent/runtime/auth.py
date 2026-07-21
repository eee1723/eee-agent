"""Runtime identity: bearer token, discovery file, and guarded cleanup.

The full token is written ONLY to ``runtime.token`` (0o600 on POSIX). The
discovery file (``runtime.json``) carries only a SHA-256 fingerprint, never
the full token. Files are published via temp-file + ``os.replace`` in the same
state directory so a write failure cannot leave a partial final file or leak
the token. Cleanup removes files only when the discovery's PID AND process
nonce both match this identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from eee_agent.runtime.protocol import PROTOCOL

TOKEN_FILENAME = "runtime.token"
DISCOVERY_FILENAME = "runtime.json"
_TOKEN_MODE = 0o600
_DISCOVERY_MODE = 0o644


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """A Runtime process's bearer token and identifying metadata."""

    token: str
    fingerprint: str
    process_nonce: str
    pid: int
    started_at: datetime


def create_identity() -> RuntimeIdentity:
    """Create a fresh Runtime identity.

    The token uses ``secrets.token_urlsafe(32)`` (32 random bytes). The
    fingerprint is the first 12 hex characters of the token's SHA-256.
    """
    token = secrets.token_urlsafe(32)
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    process_nonce = secrets.token_urlsafe(16)
    return RuntimeIdentity(
        token=token,
        fingerprint=fingerprint,
        process_nonce=process_nonce,
        pid=os.getpid(),
        started_at=datetime.now(timezone.utc),
    )


def _atomic_write(
    path: Path, data: bytes, *, mode: int
) -> None:
    """Write ``data`` to ``path`` atomically via a same-dir temp + os.replace.

    On failure the temp file is removed and the final file is never partially
    written. POSIX permissions are set on the temp before the replace.
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


def write_identity_files(
    identity: RuntimeIdentity,
    state_dir: Path | str,
    *,
    host: str,
    port: int,
) -> dict[str, object]:
    """Write the token and discovery files for ``identity``.

    Returns the discovery dict that was written. The full token appears only
    in ``runtime.token``; the discovery carries only the fingerprint.
    """
    state_dir = Path(state_dir)
    token_path = state_dir / TOKEN_FILENAME
    discovery_path = state_dir / DISCOVERY_FILENAME

    try:
        _atomic_write(
            token_path, identity.token.encode("utf-8"), mode=_TOKEN_MODE
        )

        discovery: dict[str, object] = {
            "protocol": PROTOCOL,
            "host": host,
            "port": port,
            "pid": identity.pid,
            "process_nonce": identity.process_nonce,
            "token_file": TOKEN_FILENAME,
            "token_fingerprint": identity.fingerprint,
            "started_at": identity.started_at.isoformat(),
        }
        text = json.dumps(
            discovery,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        _atomic_write(
            discovery_path, text.encode("utf-8"), mode=_DISCOVERY_MODE
        )
    except BaseException:
        # All-or-nothing: if either publish failed, remove the token file so a
        # partial state (token without discovery) is never left behind and the
        # token is not leaked to an orphaned final file.
        try:
            token_path.unlink()
        except OSError:
            pass
        raise
    return discovery


def validate_bearer(header_value: str | None, identity: RuntimeIdentity) -> bool:
    """Validate an Authorization header value against ``identity``.

    Accepts exactly one ``Bearer <token>`` credential. Malformed input
    (missing, empty, multiple credentials, wrong scheme, extra content) is
    rejected before any comparison. A well-formed credential is compared with
    ``hmac.compare_digest`` (constant-time).
    """
    if not isinstance(header_value, str):
        return False
    parts = header_value.split(" ")
    if len(parts) != 2:
        return False
    scheme, token = parts
    if scheme != "Bearer":
        return False
    if not token:
        return False
    return hmac.compare_digest(token, identity.token)


def cleanup_identity_files(
    identity: RuntimeIdentity, state_dir: Path | str
) -> None:
    """Remove this identity's token and discovery files.

    Files are removed only when the discovery's PID AND process_nonce both
    match ``identity``. Malformed or foreign discovery files are left
    untouched. Idempotent: a missing discovery file or already-removed files
    are a no-op.
    """
    state_dir = Path(state_dir)
    discovery_path = state_dir / DISCOVERY_FILENAME
    token_path = state_dir / TOKEN_FILENAME

    if not discovery_path.exists():
        return
    try:
        with open(discovery_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # Malformed discovery: never delete files we cannot identify.
        return
    if not isinstance(data, dict):
        return
    if (
        data.get("pid") != identity.pid
        or data.get("process_nonce") != identity.process_nonce
    ):
        return

    for path in (token_path, discovery_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
