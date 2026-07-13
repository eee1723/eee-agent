from __future__ import annotations

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderAdapter,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
)


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[ProviderKind, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        if adapter.kind in self._adapters:
            raise ValueError(
                f"provider adapter already registered: {adapter.kind.value}"
            )
        self._adapters[adapter.kind] = adapter

    def resolve(
        self, connection: ProviderConnection, profile: ModelProfile
    ) -> ResolvedModel:
        if profile.connection_id != connection.connection_id:
            raise ValueError(
                "profile connection_id does not match the selected connection"
            )
        try:
            adapter = self._adapters[connection.provider]
        except KeyError as exc:
            raise ValueError(
                f"no provider adapter registered for {connection.provider.value}"
            ) from exc
        model = adapter.build(connection, profile)
        return ResolvedModel(connection=connection, profile=profile, model=model)
