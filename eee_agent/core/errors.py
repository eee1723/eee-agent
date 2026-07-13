from __future__ import annotations

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
        if "." not in self.code or self.code.startswith(".") or self.code.endswith("."):
            raise ValueError("AgentError.code must be a namespaced value")
        if not self.message_for_user.strip():
            raise ValueError("AgentError.message_for_user must not be empty")

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
