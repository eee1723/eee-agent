from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage

from eee_agent.html_workflow_guard import _blocked_result, _workflow_state


_DIGEST = "a" * 64


def _request(
    name: str, messages: list[object], args: dict[str, object] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "id": "call_1", "args": args or {}},
        state={"messages": messages},
    )


def _brief(*, ready: bool = True) -> ToolMessage:
    payload: dict[str, object] = {"ok": True, "ready": ready}
    if ready:
        payload.update(
            {
                "brief_digest": _DIGEST,
                "brief": {"brief_key": "bike"},
            }
        )
    else:
        payload.update(
            {
                "question_count": 2,
                "questions": ["需要哪些组件？", "需要什么细节等级？"],
            }
        )
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        name="prepare_modeling_brief",
        tool_call_id="brief_1",
    )


def _render(*, ok: bool = True) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {
                "ok": ok,
                "image_path": "output/sketches/bike.png",
                "brief_digest": _DIGEST,
            }
        ),
        name="render_sketch",
        tool_call_id="render_1",
    )


def test_simple_modeling_request_does_not_require_html() -> None:
    messages = [HumanMessage(content="创建一个 box")]
    assert _workflow_state(messages) == (False, False, False, False)
    assert _blocked_result(_request("scratch_build", messages)) is None


def test_procedural_request_blocks_houdini_write_before_brief() -> None:
    messages = [HumanMessage(content="做一个程序化自行车")]
    blocked = _blocked_result(_request("scratch_build", messages))
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == "workflow.modeling_brief_required"


def test_explicit_modeling_request_also_requires_brief() -> None:
    messages = [HumanMessage(content="帮我做一个自行车模型")]
    blocked = _blocked_result(_request("render_sketch", messages))
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == "workflow.modeling_brief_required"


def test_procedural_request_blocks_render_before_ready_brief() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车"),
        _brief(ready=False),
    ]
    blocked = _blocked_result(
        _request("render_sketch", messages, {"brief_digest": _DIGEST})
    )
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == "workflow.modeling_brief_required"


def test_render_requires_exact_ready_brief_digest() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车"),
        _brief(),
    ]
    missing = _blocked_result(_request("render_sketch", messages))
    mismatch = _blocked_result(
        _request("render_sketch", messages, {"brief_digest": "b" * 64})
    )
    assert missing is not None
    assert mismatch is not None
    assert json.loads(missing.content)["code"] == "workflow.modeling_brief_mismatch"
    assert json.loads(mismatch.content)["code"] == "workflow.modeling_brief_mismatch"


def test_ready_brief_with_matching_digest_opens_render_gate() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车"),
        _brief(),
    ]
    assert (
        _blocked_result(
            _request("render_sketch", messages, {"brief_digest": _DIGEST})
        )
        is None
    )


def test_failed_render_does_not_open_gate() -> None:
    messages = [
        HumanMessage(content="做一个参数化城堡"),
        _brief(),
        _render(ok=False),
    ]
    assert _workflow_state(messages) == (True, True, False, False)


def test_successful_render_still_requires_later_user_approval() -> None:
    messages = [
        HumanMessage(content="做一个 parametric bicycle"),
        _brief(),
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
        _brief(),
        _render(),
        HumanMessage(content="可以"),
    ]
    assert _workflow_state(messages) == (True, True, True, True)
    assert _blocked_result(_request("scratch_build", messages)) is None
    assert _blocked_result(_request("scratch_commit", messages)) is None


def test_approval_before_render_does_not_open_gate() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车，可以直接做"),
        _brief(),
        _render(),
    ]
    assert _workflow_state(messages) == (True, True, True, False)


def test_user_feedback_requires_recompiled_brief_before_rerender() -> None:
    messages = [
        HumanMessage(content="做一个程序化自行车"),
        _brief(),
        _render(),
        HumanMessage(content="增加刹车和线缆后重新渲染"),
    ]
    blocked = _blocked_result(
        _request("render_sketch", messages, {"brief_digest": _DIGEST})
    )
    assert blocked is not None
    assert json.loads(blocked.content)["code"] == "workflow.modeling_brief_required"
