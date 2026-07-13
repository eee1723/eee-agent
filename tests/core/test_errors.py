import pytest
from eee_agent.core.errors import AgentError, AgentException, ErrorCategory

def test_agent_error_serializes_without_exception_objects() -> None:
    error = AgentError(code="provider.missing_key", category=ErrorCategory.PROVIDER_CONTRACT, message_for_user="DeepSeek credentials are not configured.", retryable=False, requires_user_action=True, suggested_actions=("Configure DEEPSEEK_API_KEY.",))
    assert error.to_dict() == {"code": "provider.missing_key", "category": "provider_contract", "message_for_user": "DeepSeek credentials are not configured.", "technical_detail_ref": None, "retryable": False, "requires_user_action": True, "scene_may_have_changed": False, "suggested_actions": ["Configure DEEPSEEK_API_KEY."], "cause_chain": []}

def test_agent_exception_exposes_structured_error() -> None:
    error = AgentError(code="internal.invariant", category=ErrorCategory.INTERNAL_INVARIANT, message_for_user="An internal invariant failed.")
    exc = AgentException(error)
    assert exc.error is error
    assert str(exc) == "An internal invariant failed."

def test_agent_error_requires_namespaced_code() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        AgentError(code="bad", category=ErrorCategory.PROTOCOL, message_for_user="Bad code.")
