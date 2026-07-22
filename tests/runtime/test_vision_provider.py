from __future__ import annotations

import asyncio
import base64
import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import Field

from eee_agent.config import VisionConfig
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.vision.contracts import VisionRequest
from eee_agent.vision.provider import LangChainVisionProvider, build_vision_provider


IMAGE = b"\x89PNG\r\n\x1a\nverified-image"
REPORT = {
    "summary": "Silhouette matches.",
    "observations": ["One bounded observation."],
    "confidence": 0.9,
    "advisory_passed": True,
}


class _RecordingModel(BaseChatModel):
    reply: str
    calls: list[object] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-vision-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    async def ainvoke(self, messages, config=None, *, stop=None, **kwargs):  # type: ignore[override]
        self.calls.append(messages)
        return AIMessage(content=self.reply)


def _request(*, size_bytes: int = len(IMAGE), path: str = "never/open/me.png") -> VisionRequest:
    return VisionRequest(
        "vision-request-1",
        ArtifactRef(
            artifact_id="art_" + "a" * 32,
            relative_path=path,
            sha256="b" * 64,
            media_type="image/png",
            size_bytes=size_bytes,
        ),
        "Check the silhouette against the approved reference.",
    )


def _evaluate(reply: str, *, image: bytes = IMAGE) -> tuple[dict[str, object], _RecordingModel]:
    model = _RecordingModel(reply=reply)
    provider = LangChainVisionProvider(
        model,
        provider_id="openai",
        max_image_bytes=8 * 1024 * 1024,
    )
    result = asyncio.run(provider.evaluate(_request(size_bytes=len(image)), image))
    return dict(result), model


def test_capability_is_explicit_and_bounded() -> None:
    model = _RecordingModel(reply=json.dumps(REPORT))
    provider = LangChainVisionProvider(
        model, provider_id="anthropic", max_image_bytes=4096
    )
    capability = asyncio.run(provider.capability())
    assert capability.provider_id == "anthropic"
    assert capability.available is True
    assert capability.media_types == ("image/png", "image/jpeg", "image/webp")
    assert capability.max_image_bytes == 4096
    assert capability.reason_code is None


def test_evaluate_uses_only_supplied_bytes_and_one_bounded_instruction() -> None:
    result, model = _evaluate(json.dumps(REPORT))
    assert result == REPORT
    assert len(model.calls) == 1
    messages = model.calls[0]
    assert type(messages) is list and len(messages) == 1
    message = messages[0]
    assert type(message) is HumanMessage
    content = message.content
    assert type(content) is list and len(content) == 2
    text_block, image_block = content
    assert type(text_block) is dict and text_block["type"] == "text"
    assert "summary" in text_block["text"]
    assert "Check the silhouette" in text_block["text"]
    assert type(image_block) is dict and image_block["type"] == "image_url"
    url = image_block["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(IMAGE).decode("ascii")


def test_evaluate_accepts_one_outer_json_fence() -> None:
    fenced = "```json\n" + json.dumps(REPORT) + "\n```"
    result, _ = _evaluate(fenced)
    assert result == REPORT


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not json",
        "[]",
        "prefix " + json.dumps(REPORT),
        "```text\n" + json.dumps(REPORT) + "\n```",
        " " * 16_385,
    ],
)
def test_evaluate_rejects_empty_non_object_or_unbounded_output(reply: str) -> None:
    with pytest.raises(ValueError, match="response"):
        _evaluate(reply)


def test_evaluate_rejects_image_over_configured_limit() -> None:
    model = _RecordingModel(reply=json.dumps(REPORT))
    provider = LangChainVisionProvider(
        model, provider_id="openai", max_image_bytes=len(IMAGE) - 1
    )
    with pytest.raises(ValueError, match="image bytes"):
        asyncio.run(provider.evaluate(_request(), IMAGE))
    assert model.calls == []


def test_build_provider_uses_existing_registry_resolution(monkeypatch) -> None:
    import eee_agent.vision.provider as provider_module

    model = _RecordingModel(reply=json.dumps(REPORT))
    captured: list[object] = []

    class _Resolved:
        def __init__(self) -> None:
            self.model = model

    def fake_resolve(config):
        captured.append(config)
        return _Resolved()

    monkeypatch.setattr(provider_module, "resolve_model", fake_resolve)
    config = VisionConfig("openai", "gpt-4.1", 4096, 12.5)
    provider = build_vision_provider(config)
    assert isinstance(provider, LangChainVisionProvider)
    assert len(captured) == 1
    resolved_config = captured[0]
    assert resolved_config.provider == "openai"
    assert resolved_config.model == "gpt-4.1"
    assert resolved_config.thinking_enabled is False
    assert resolved_config.effort is None
    assert resolved_config.max_output_tokens == 2048
