import json

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

def _make_agent_error(**overrides: object) -> AgentError:
    values: dict[str, object] = {
        "code": "protocol.invalid_payload",
        "category": ErrorCategory.PROTOCOL,
        "message_for_user": "The payload is invalid.",
    }
    values.update(overrides)
    return AgentError(**values)  # type: ignore[arg-type]

def test_agent_error_copies_mutable_sequence_inputs() -> None:
    actions = ["Retry with a valid payload."]
    causes = ["provider.invalid_response"]
    error = _make_agent_error(suggested_actions=actions, cause_chain=causes)

    actions.append("Mutated later.")
    causes.append("provider.mutated_later")

    assert error.suggested_actions == ("Retry with a valid payload.",)
    assert error.cause_chain == ("provider.invalid_response",)

def test_agent_error_requires_error_category_instance() -> None:
    with pytest.raises(ValueError, match="category"):
        _make_agent_error(category="protocol")

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retryable", 1),
        ("requires_user_action", None),
        ("scene_may_have_changed", "false"),
    ],
)
def test_agent_error_requires_boolean_flags(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _make_agent_error(**{field: value})

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", 42),
        ("message_for_user", 42),
        ("message_for_user", "  "),
        ("technical_detail_ref", 42),
        ("technical_detail_ref", "\n"),
    ],
)
def test_agent_error_requires_valid_string_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _make_agent_error(**{field: value})

@pytest.mark.parametrize("code", ["a..b", "a. b", "a/b.c", "a.b\n"])
def test_agent_error_rejects_malformed_namespaced_codes(code: str) -> None:
    with pytest.raises(ValueError, match="namespaced"):
        _make_agent_error(code=code)

@pytest.mark.parametrize("field", ["suggested_actions", "cause_chain"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(object(), id="object"),
        pytest.param("single string", id="string-container"),
        pytest.param([object()], id="object-entry"),
        pytest.param([42], id="non-string-entry"),
        pytest.param(["  "], id="blank-entry"),
    ],
)
def test_agent_error_rejects_invalid_string_sequences(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        _make_agent_error(**{field: value})

def test_agent_error_to_dict_is_strict_json_serializable() -> None:
    error = _make_agent_error(
        technical_detail_ref="artifacts/provider-detail.json",
        retryable=True,
        suggested_actions=["Retry the request."],
        cause_chain=["provider.timeout"],
    )
    payload = error.to_dict()

    assert json.loads(json.dumps(payload, allow_nan=False)) == payload
