from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from eee_agent.core.ids import IdKind, require_id


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    relative_path: str
    sha256: str
    media_type: str
    size_bytes: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_id(self.artifact_id, IdKind.ARTIFACT)
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.relative_path
        ):
            raise ValueError(f"unsafe artifact path: {self.relative_path!r}")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must contain exactly 64 lowercase hex characters")
        if not self.media_type.strip() or "/" not in self.media_type:
            raise ValueError("media_type must be a MIME type")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
        }
