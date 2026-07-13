from __future__ import annotations

import os

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory


def resolve_secret(secret_ref: str) -> str:
    if not secret_ref.startswith("env:"):
        raise AgentException(
            AgentError(
                code="provider.unsupported_secret_ref",
                category=ErrorCategory.PROVIDER_CONTRACT,
                message_for_user=(
                    "This secret source is not supported by Foundation."
                ),
            )
        )

    variable = secret_ref.removeprefix("env:")
    value = os.getenv(variable)
    if value:
        return value
    raise AgentException(
        AgentError(
            code="provider.missing_key",
            category=ErrorCategory.PROVIDER_CONTRACT,
            message_for_user=f"Required credential {variable} is not configured.",
            requires_user_action=True,
            suggested_actions=(f"Configure {variable} for the next run.",),
        )
    )
