"""Tests for the Session auto-title generator."""

from __future__ import annotations

import asyncio
import functools
from typing import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from eee_agent.runtime.titles import generate_session_title


def async_test(coro: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    @functools.wraps(coro)
    def wrapper() -> None:
        asyncio.run(coro())

    return wrapper


class _FakeModel(BaseChatModel):
    """Returns a canned response, optionally after a delay."""

    reply: str = "ok"
    delay: float = 0.0
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        raise NotImplementedError

    async def ainvoke(self, messages, config=None, *, stop=None, **kwargs):  # type: ignore[override]
        if self.fail:
            raise RuntimeError("provider unavailable")
        if self.delay:
            await asyncio.sleep(self.delay)
        return _FakeMessage(self.reply)


class _FakeMessage(BaseMessage):
    """Minimal message exposing .content."""

    @property
    def type(self) -> str:
        return "ai"

    @property
    def content(self) -> str:  # type: ignore[override]
        return self.additional_kwargs.get("text", "")


def _msg(text: str) -> _FakeMessage:
    m = _FakeMessage("")  # type: ignore[call-arg]
    m.additional_kwargs = {"text": text}
    return m


@async_test
async def test_generates_title_from_user_prompt() -> None:
    model = _FakeModel(reply="带栏杆的楼梯")  # type: ignore[call-arg]
    title = await generate_session_title(model, "帮我建一个带栏杆的楼梯")
    assert title == "带栏杆的楼梯"


@async_test
async def test_strips_quotes_and_title_prefix() -> None:
    model = _FakeModel(reply='"Title: A bracket shelf."')  # type: ignore[call-arg]
    title = await generate_session_title(model, "build a shelf")
    assert title == "A bracket shelf."
    assert len(title) <= 40


@async_test
async def test_truncates_overlong_title() -> None:
    model = _FakeModel(reply="x" * 200)  # type: ignore[call-arg]
    title = await generate_session_title(model, "do something")
    assert len(title) == 40


@async_test
async def test_returns_none_on_empty_user_input() -> None:
    model = _FakeModel(reply="should not happen")  # type: ignore[call-arg]
    assert await generate_session_title(model, "") is None
    assert await generate_session_title(model, "   ") is None


@async_test
async def test_returns_none_on_provider_failure() -> None:
    model = _FakeModel(fail=True)  # type: ignore[call-arg]
    assert await generate_session_title(model, "build a table") is None


@async_test
async def test_returns_none_on_timeout() -> None:
    model = _FakeModel(delay=10.0)  # type: ignore[call-arg]
    # The default timeout is 8s; sleeping 10s would stall the suite, so verify
    # the guard works via the public timeout path with a short window instead.
    import eee_agent.runtime.titles as titles
    original = titles._TIMEOUT_SECONDS
    titles._TIMEOUT_SECONDS = 0.05
    try:
        assert await generate_session_title(model, "build a table") is None
    finally:
        titles._TIMEOUT_SECONDS = original


@async_test
async def test_includes_final_response_in_context() -> None:
    captured: list[BaseMessage] = []

    class _CaptureModel(_FakeModel):
        async def ainvoke(self, messages, config=None, *, stop=None, **kwargs):  # type: ignore[override]
            captured.extend(messages)
            return _msg("done")

    model = _CaptureModel(reply="done")  # type: ignore[call-arg]
    await generate_session_title(
        model, "build a table", final_response="I built a parametric table.")
    # The human message should carry both the prompt and the reply summary.
    human = [m for m in captured if m.type == "human"]
    assert human and "build a table" in human[-1].content
    assert "parametric table" in human[-1].content
