from langchain_core.language_models.fake_chat_models import FakeListChatModel

import pytest

from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ModelRole,
    ModelVerification,
    ProviderAdapter,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
    RoleBindings,
    ThinkingEffort,
    Transport,
    VerificationCheck,
    VerificationStatus,
)
from eee_agent.providers.registry import ProviderRegistry


class FakeAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(
        self, connection: ProviderConnection, profile: ModelProfile
    ) -> FakeListChatModel:
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


def test_registry_resolves_connection_profile_and_model() -> None:
    adapter: ProviderAdapter = FakeAdapter()
    registry = ProviderRegistry()
    registry.register(adapter)

    resolved = registry.resolve(connection(), profile())

    assert resolved.connection == connection()
    assert resolved.profile == profile()
    assert isinstance(resolved, ResolvedModel)
    assert isinstance(resolved.model, FakeListChatModel)


def test_registry_rejects_duplicate_provider_adapter() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())

    with pytest.raises(
        ValueError, match="provider adapter already registered: deepseek"
    ):
        registry.register(FakeAdapter())


def test_registry_rejects_profile_connection_mismatch() -> None:
    mismatched_profile = ModelProfile(
        profile_id="primary",
        connection_id="another-connection",
        model_name="deepseek-v4-pro",
        capabilities=profile().capabilities,
    )

    with pytest.raises(ValueError, match="connection_id"):
        ProviderRegistry().resolve(connection(), mismatched_profile)


def test_role_bindings_and_model_verification_contracts() -> None:
    bindings = RoleBindings(
        primary_profile_id="primary", vision_profile_id="vision"
    )
    verification = ModelVerification(
        status=VerificationStatus.VERIFIED,
        requested_model_name="deepseek-v4-pro",
        actual_model_name="deepseek-v4-pro",
        checks=(
            VerificationCheck(
                name="tool_replay", passed=True, detail="Replay succeeded."
            ),
        ),
    )

    assert bindings.profile_for(ModelRole.PRIMARY) == "primary"
    assert bindings.profile_for(ModelRole.VISION) == "vision"
    assert verification.status is VerificationStatus.VERIFIED
    assert verification.checks[0].name == "tool_replay"
    assert verification.checks[0].passed is True
