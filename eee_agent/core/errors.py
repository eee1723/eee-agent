from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    PERMISSION = "permission"
    STALE_SCENE = "stale_scene"
    PROVIDER_CONTRACT = "provider_contract"
    PROVIDER_TRANSIENT = "provider_transient"
    HOUDINI_COOK = "houdini_cook"
    HOUDINI_BRIDGE = "houdini_bridge"
    ARTIFACT = "artifact"
    PROTOCOL = "protocol"
    INTERNAL_INVARIANT = "internal_invariant"
    CRITICAL_RECOVERY = "critical_recovery"


# Codes are dot-separated lowercase segments containing letters, digits, or underscores.
_ERROR_CODE_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")


def _normalize_string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"AgentError.{field_name} must be a sequence of strings")
    items = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValueError(
            f"AgentError.{field_name} must contain only non-empty strings"
        )
    return items


@dataclass(frozen=True, slots=True)
class AgentError:
    code: str
    category: ErrorCategory
    message_for_user: str
    technical_detail_ref: str | None = None
    retryable: bool = False
    requires_user_action: bool = False
    scene_may_have_changed: bool = False
    suggested_actions: tuple[str, ...] = ()
    cause_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _ERROR_CODE_RE.fullmatch(self.code):
            raise ValueError("AgentError.code must be a namespaced value")
        if type(self.category) is not ErrorCategory:
            raise ValueError("AgentError.category must be an ErrorCategory")
        if not isinstance(self.message_for_user, str) or not self.message_for_user.strip():
            raise ValueError("AgentError.message_for_user must not be empty")
        if self.technical_detail_ref is not None and (
            not isinstance(self.technical_detail_ref, str)
            or not self.technical_detail_ref.strip()
        ):
            raise ValueError(
                "AgentError.technical_detail_ref must be a non-empty string or None"
            )
        for field_name in (
            "retryable",
            "requires_user_action",
            "scene_may_have_changed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"AgentError.{field_name} must be a bool")
        object.__setattr__(
            self,
            "suggested_actions",
            _normalize_string_sequence(self.suggested_actions, "suggested_actions"),
        )
        object.__setattr__(
            self,
            "cause_chain",
            _normalize_string_sequence(self.cause_chain, "cause_chain"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message_for_user": self.message_for_user,
            "technical_detail_ref": self.technical_detail_ref,
            "retryable": self.retryable,
            "requires_user_action": self.requires_user_action,
            "scene_may_have_changed": self.scene_may_have_changed,
            "suggested_actions": list(self.suggested_actions),
            "cause_chain": list(self.cause_chain),
        }


class AgentException(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        self.error = error
        super().__init__(error.message_for_user)
