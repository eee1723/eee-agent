"""Bounded, read-only Knowledge Graph integration for Runtime.

The Runtime owns the cache *location* and startup classification, while the
knowledge package owns SQLite/query validation.  This adapter deliberately
returns plain dictionaries so the model never receives a store, connection or
machine-local path.  A missing or unhealthy cache is advisory: Runtime still
starts and knowledge calls return a bounded unavailable result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

from eee_agent.knowledge.api import GetRequest, KnowledgeErrorCode, SearchRequest
from eee_agent.knowledge.service import KnowledgeService


class KnowledgeStatusCode(StrEnum):
    MISSING = "missing"
    STALE = "stale"
    CORRUPT = "corrupt"
    SCHEMA_MISMATCH = "schema_mismatch"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class KnowledgeRuntimeStatus:
    code: KnowledgeStatusCode
    available: bool
    manifest_sha256: str | None = None
    schema_version: int | None = None
    houdini_build: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.code.value,
            "available": self.available,
            "manifest_sha256": self.manifest_sha256,
            "schema_version": self.schema_version,
            "houdini_build": self.houdini_build,
            "message": self.message,
        }


def _error_result(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "message": message}


class KnowledgeRuntime:
    """Read-only Runtime facade over a shared Knowledge Graph cache."""

    def __init__(
        self,
        path: Path | str,
        *,
        stale_checker: Callable[[Mapping[str, object]], bool] | None = None,
        live_catalog: Callable[[str], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self._stale_checker = stale_checker or (lambda _metadata: False)
        self._live_catalog = live_catalog
        self._status = self._classify()

    @property
    def status(self) -> KnowledgeRuntimeStatus:
        return self._status

    @property
    def startup_allowed(self) -> bool:
        # Knowledge is advisory-only; cache failures never prevent Runtime.
        return True

    def refresh(self) -> KnowledgeRuntimeStatus:
        self._status = self._classify()
        return self._status

    def _classify(self) -> KnowledgeRuntimeStatus:
        if not self.path.is_file():
            return KnowledgeRuntimeStatus(
                KnowledgeStatusCode.MISSING, False, message="knowledge cache is missing"
            )
        service = KnowledgeService(self.path, stale_checker=self._stale_checker)
        status = service.status()
        provenance = status.provenance
        if status.available:
            code = KnowledgeStatusCode.READY
            available = True
        elif status.code is KnowledgeErrorCode.KB_STALE:
            code = KnowledgeStatusCode.STALE
            available = False
        elif status.code is KnowledgeErrorCode.KB_SCHEMA_MISMATCH:
            code = KnowledgeStatusCode.SCHEMA_MISMATCH
            available = False
        elif status.code is KnowledgeErrorCode.KB_CORRUPT:
            code = KnowledgeStatusCode.CORRUPT
            available = False
        else:
            code = KnowledgeStatusCode.CORRUPT
            available = False
        return KnowledgeRuntimeStatus(
            code,
            available,
            provenance.manifest_sha256,
            provenance.kb_schema_version,
            provenance.houdini_build,
            status.message,
        )

    def snapshot_fields(self) -> dict[str, object]:
        status = self._status
        return {
            "kb_manifest_sha256": status.manifest_sha256,
            "kb_schema_version": status.schema_version,
            "houdini_build": status.houdini_build,
            "knowledge_status": status.code.value,
        }

    def search(self, query: str, *, limit: int = 5) -> dict[str, object]:
        if not self.status.available:
            return _error_result("kb_unavailable", self._unavailable_message())
        bounded_query = query if isinstance(query, str) else ""
        bounded_limit = min(max(int(limit), 1), 10)
        try:
            response = KnowledgeService(
                self.path, stale_checker=self._stale_checker
            ).search(SearchRequest(query=bounded_query, limit=bounded_limit))
            payload = response.to_dict()
            payload["ok"] = bool(payload.get("ok"))
            return payload
        except Exception:
            return _error_result("kb_unavailable", "knowledge cache unavailable")

    def get(self, entity_id: str, *, max_body_bytes: int = 8_000) -> dict[str, object]:
        if not self.status.available:
            return _error_result("kb_unavailable", self._unavailable_message())
        bounded_entity = entity_id if isinstance(entity_id, str) else ""
        bounded_bytes = min(max(int(max_body_bytes), 1), 8_000)
        try:
            payload = KnowledgeService(
                self.path, stale_checker=self._stale_checker
            ).get(GetRequest(entity_id=bounded_entity, max_chars=bounded_bytes)).to_dict()
            body = payload.get("body")
            if isinstance(body, str):
                payload["body"] = body[:bounded_bytes]
            return payload
        except Exception:
            return _error_result("kb_unavailable", "knowledge cache unavailable")

    def can_create(self, catalog_key: str) -> bool:
        """Knowledge text never grants creatability; live catalog is authority."""
        if self._live_catalog is None or not isinstance(catalog_key, str):
            return False
        try:
            return bool(self._live_catalog(catalog_key))
        except Exception:
            return False

    def _unavailable_message(self) -> str:
        return f"knowledge cache is {self.status.code.value}"


__all__ = ["KnowledgeRuntime", "KnowledgeRuntimeStatus", "KnowledgeStatusCode"]
