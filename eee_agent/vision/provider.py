"""LangChain-backed advisory Vision provider over already-verified bytes."""

from __future__ import annotations

import base64
import json
from typing import Mapping

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from eee_agent.config import LlmConfig, VisionConfig
from eee_agent.core.strict_json import DuplicateKeyError, reject_duplicate_keys
from eee_agent.providers.factory import resolve_model
from eee_agent.vision.contracts import ProviderCapability, VisionRequest


_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")
_MAX_RESPONSE_CHARS = 16 * 1024

# Shared dup-key-rejecting hook (single authority: eee_agent.core.strict_json).
_DuplicateKeyError = DuplicateKeyError
_reject_duplicate_keys = reject_duplicate_keys


def _response_text(response: object) -> str:
    content: object = getattr(response, "content", None)
    if type(content) is str:
        text = content
    elif type(content) is list:
        parts: list[str] = []
        for block in content:
            if type(block) is str:
                parts.append(block)
            elif type(block) is dict and block.get("type") == "text":
                value = block.get("text")
                if type(value) is str:
                    parts.append(value)
        text = "".join(parts)
    else:
        raise ValueError("visual provider response text is invalid")
    if not text or len(text) > _MAX_RESPONSE_CHARS:
        raise ValueError("visual provider response exceeds the text budget")
    return text


def _unfence_json(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("visual provider response is empty")
    if not stripped.startswith("```"):
        if "```" in stripped:
            raise ValueError("visual provider response fence is invalid")
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0] != "```json" or lines[-1] != "```":
        raise ValueError("visual provider response fence is invalid")
    payload = "\n".join(lines[1:-1]).strip()
    if not payload or "```" in payload:
        raise ValueError("visual provider response fence is invalid")
    return payload


def _parse_response(text: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(
            _unfence_json(text), object_pairs_hook=_reject_duplicate_keys
        )
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ValueError("visual provider response is not strict JSON") from exc
    if type(parsed) is not dict:
        raise ValueError("visual provider response must be one JSON object")
    return parsed


class LangChainVisionProvider:
    """Outer adapter that sends verified image bytes to one chat model."""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        provider_id: str,
        max_image_bytes: int,
    ) -> None:
        if not isinstance(model, BaseChatModel):
            raise TypeError("model must be a BaseChatModel")
        if type(provider_id) is not str or not provider_id or len(provider_id) > 64:
            raise ValueError("provider_id must be a bounded string")
        if type(max_image_bytes) is not int or not 1 <= max_image_bytes <= 16_777_216:
            raise ValueError("max_image_bytes is invalid")
        self._model = model
        self._provider_id = provider_id
        self._max_image_bytes = max_image_bytes

    async def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider_id=self._provider_id,
            available=True,
            media_types=_MEDIA_TYPES,
            max_image_bytes=self._max_image_bytes,
            reason_code=None,
        )

    async def evaluate(
        self,
        request: VisionRequest,
        image_bytes: bytes,
    ) -> Mapping[str, object]:
        if type(request) is not VisionRequest:
            raise TypeError("request must be an exact VisionRequest")
        if (
            type(image_bytes) is not bytes
            or not image_bytes
            or len(image_bytes) > self._max_image_bytes
            or len(image_bytes) != request.artifact.size_bytes
        ):
            raise ValueError("verified image bytes are invalid")
        if request.artifact.media_type not in _MEDIA_TYPES:
            raise ValueError("visual artifact media type is unsupported")

        instruction = (
            "Evaluate only the supplied image and the user instruction below. "
            "Return exactly one JSON object with exactly four fields: "
            '"summary" (string), "observations" (array of strings), '
            '"confidence" (number from 0 to 1), and "advisory_passed" '
            "(boolean). Do not add fields, Markdown, or surrounding prose. "
            "Do not infer evidence outside the supplied image.\n\n"
            f"User instruction: {request.instruction}"
        )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        message = HumanMessage(
            content=[
                {"type": "text", "text": instruction},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{request.artifact.media_type};base64,{encoded}"
                    },
                },
            ]
        )
        response = await self._model.ainvoke([message])
        return _parse_response(_response_text(response))


def build_vision_provider(config: VisionConfig) -> LangChainVisionProvider:
    if type(config) is not VisionConfig:
        raise TypeError("config must be an exact VisionConfig")
    resolved = resolve_model(
        LlmConfig(
            provider=config.provider,
            model=config.model,
            thinking_enabled=False,
            effort=None,
            max_output_tokens=2048,
        )
    )
    return LangChainVisionProvider(
        resolved.model,
        provider_id=config.provider,
        max_image_bytes=config.max_image_bytes,
    )
