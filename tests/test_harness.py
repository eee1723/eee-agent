import pytest

from eee_agent.app import build_agent
from eee_agent.runtime.agent_tools import build_read_only_tools


@pytest.mark.parametrize(
    ("provider", "key_env"),
    [
        # DeepSeek V4 is built on ChatAnthropic, so Deep Agents resolves its
        # harness provider as "anthropic".
        ("deepseek", "DEEPSEEK_API_KEY"),
        # Standard OpenAI is a separate harness key; exercise it so the defensive
        # register_harness_profile("openai", ...) line cannot be silently deleted.
        ("openai", "OPENAI_API_KEY"),
    ],
)
def test_build_agent_has_no_implicit_general_purpose_subagent(
    monkeypatch, provider: str, key_env: str
) -> None:
    # Pin the provider so the test is self-contained and does not depend on a
    # machine-local .env value for EEE_LLM_PROVIDER.
    monkeypatch.setenv("EEE_LLM_PROVIDER", provider)
    monkeypatch.setenv(key_env, "unit-test-key")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    # Neutralize machine-local model/thinking overrides loaded from .env: the
    # parametrized provider must resolve its own defaults (e.g. a DeepSeek
    # thinking-enabled profile is invalid for the standard OpenAI connection).
    monkeypatch.delenv("EEE_LLM_MODEL", raising=False)
    monkeypatch.delenv("EEE_LLM_THINKING", raising=False)
    monkeypatch.delenv("EEE_LLM_EFFORT", raising=False)
    # Foundation ships without optional tracing deps (openinference/Phoenix are
    # later milestones). Neutralize a machine-local EEE_TRACING=phoenix loaded by
    # load_dotenv() so build_agent() does not try to instrument Phoenix here.
    monkeypatch.delenv("EEE_TRACING", raising=False)

    graph = build_agent(tools=build_read_only_tools())
    # Deep Agents has no public tool-introspection API. This deliberately checks
    # the compiled ToolNode so a dependency upgrade fails loudly if the task tool
    # or its inherited Houdini tools return.
    tool_names = set(graph.nodes["tools"].bound._tools_by_name)  # noqa: SLF001
    assert "task" not in tool_names
    assert "scene_status" in tool_names
