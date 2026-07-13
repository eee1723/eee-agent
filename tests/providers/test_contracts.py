import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ModelRole,
    ModelVerification,
    ProviderConnection,
    ProviderKind,
    RoleBindings,
    ThinkingEffort,
    Transport,
    VerificationCheck,
    VerificationStatus,
)
from eee_agent.providers.registry import ProviderRegistry


class FakeAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(self, connection: ProviderConnection, profile: ModelProfile):
        return FakeListChatModel(responses=[profile.model_name])


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id="deepseek-main",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url="https://api.deepseek.com/anthropic",
        secret_ref="env:DEEPSEEK_API_KEY",
    )


def profile() -> ModelProfile:
    return ModelProfile(
        profile_id="primary",
        connection_id="deepseek-main",
        model_name="deepseek-v4-pro",
        capabilities=ModelCapabilities(
            streaming=True,
            thinking=True,
            tool_calling=True,
            structured_output=True,
            image_input=False,
        ),
        thinking_enabled=True,
        effort=ThinkingEffort.MAX,
        max_output_tokens=8192,
    )


def test_registry_resolves_adapter_and_frozen_snapshot() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())

    resolved = registry.resolve(connection(), profile())

    assert resolved.connection == connection()
    assert resolved.profile == profile()
    assert isinstance(resolved.model, FakeListChatModel)


def test_registry_rejects_duplicate_adapter() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeAdapter())


def test_registry_rejects_profile_connection_mismatch() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    wrong = ModelProfile(
        profile_id="wrong",
        connection_id="another-connection",
        model_name="deepseek-v4-pro",
        capabilities=profile().capabilities,
    )

    with pytest.raises(ValueError, match="connection_id"):
        registry.resolve(connection(), wrong)


def test_role_bindings_and_verification_are_provider_neutral() -> None:
    bindings = RoleBindings(primary_profile_id="primary", vision_profile_id=None)
    verification = ModelVerification(
        status=VerificationStatus.VERIFIED,
        requested_model_name="deepseek-v4-pro",
        actual_model_name="deepseek-v4-pro",
        checks=(
            VerificationCheck(name="tool_replay", passed=True, detail=None),
        ),
    )

    assert bindings.profile_for(ModelRole.PRIMARY) == "primary"
    assert bindings.profile_for(ModelRole.VISION) is None
    assert verification.status is VerificationStatus.VERIFIED
