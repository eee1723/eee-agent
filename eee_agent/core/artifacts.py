from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from eee_agent.core.ids import IdKind, require_id


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Foundation v1 accepts concrete RFC token type/subtype values without parameters.
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'+.^_`|~-]+/[A-Za-z0-9!#$%&'+.^_`|~-]+$"
)
# Windows reserves these device basenames even when a component has an extension.
_WINDOWS_RESERVED_COMPONENT_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?",
    flags=re.IGNORECASE | re.ASCII,
)
# NTFS ADS separators, Windows punctuation restrictions, and ASCII controls.
_WINDOWS_INVALID_COMPONENT_CHAR_RE = re.compile(r'[\x00-\x1f<>:"|?*]')


def _is_unsafe_windows_component(component: str) -> bool:
    # Python 3.11 is pinned; its component-level check covers console, padded,
    # and superscript device aliases beyond the defensive explicit regex.
    windows_component = PureWindowsPath(component)
    return (
        _WINDOWS_INVALID_COMPONENT_CHAR_RE.search(component) is not None
        or component.endswith((" ", "."))
        or _WINDOWS_RESERVED_COMPONENT_RE.fullmatch(component) is not None
        or windows_component.is_reserved()
    )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    relative_path: str
    sha256: str
    media_type: str
    size_bytes: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str):
            raise ValueError("artifact_id must be a string")
        require_id(self.artifact_id, IdKind.ARTIFACT)
        if not isinstance(self.relative_path, str):
            raise ValueError(f"unsafe artifact path: {self.relative_path!r}")
        path = PurePosixPath(self.relative_path)
        windows_path = PureWindowsPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or ".." in path.parts
            or "\\" in self.relative_path
            or "\0" in self.relative_path
            or path == PurePosixPath(".")
            or str(path) != self.relative_path
        ):
            raise ValueError(f"unsafe artifact path: {self.relative_path!r}")
        if any(_is_unsafe_windows_component(component) for component in path.parts):
            raise ValueError(f"unsafe artifact path: {self.relative_path!r}")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must contain exactly 64 lowercase hex characters")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE_RE.fullmatch(
            self.media_type
        ):
            raise ValueError("media_type must be a concrete MIME type without parameters")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be the supported integer value 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
        }
