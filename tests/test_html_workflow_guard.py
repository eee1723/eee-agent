from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from eee_agent.html_workflow_guard import _blocked_result, _workflow_state


def _request(name: str, messages: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "id": "call_1", "args": {}},
        state={"messages": messages},
    )


def _render(*, ok: bool = True) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {"ok": ok, "image_path": "output/sketches/bike.png"}
        ),
        name="render_sketch",
        tool_call_id="render_1",
    )


def test_simple_modeling_request_does_not_require_html() -> None:
    messages = [HumanMessage(content="创建一个 box")]
    assert _workflow_state(messages) == (False, False, False)
    assert _blocked_result(_request("scratch_build", messages)) is None


def test_procedural_request_blocks_houdini_write_before_render() -> None:
    messages = [HumanMessage(content="做一个程序化自行车")]
    blocked = _blocked_result(_request("scratch_build", messages))
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == "workflow.html_sketch_required"


def test_failed_render_does_not_open_gate() -> None:
    messages = [
        HumanMessage(content="做一个参数化城堡"),
        _render(ok=False),
    ]
    assert _workflow_state(messages) == (True, False, False)


def test_successful_render_still_requires_later_user_approval() -> None:
    messages = [
        HumanMessage(content="做一个 parametric bicycle"),
        _render(),
    ]
    blocked = _blocked_result(_request("scratch_commit", messages))
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == (
        "workflow.sketch_approval_required"
    )


def test_later_short_approval_opens_build_and_commit_gate() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车"),
        _render(),
        HumanMessage(content="可以"),
    ]
    assert _workflow_state(messages) == (True, True, True)
    assert _blocked_result(_request("scratch_build", messages)) is None
    assert _blocked_result(_request("scratch_commit", messages)) is None


def test_approval_before_render_does_not_open_gate() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车，可以直接做"),
        _render(),
    ]
    assert _workflow_state(messages) == (True, True, False)
