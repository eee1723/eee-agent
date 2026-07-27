from eee_agent.panel.runtime_state import (
    PanelClientError,
    format_task_graph_steps,
    parse_task_graph_list,
)


def test_task_graph_parser_and_formatter() -> None:
    parsed = parse_task_graph_list(
        {"steps": [{"seq": 1, "tool": "scratch_build", "purpose": "build",
                    "status": "committed", "node_count": 2}]}
    )
    assert format_task_graph_steps(parsed) == "1. [committed] build (2 nodes)"
    assert format_task_graph_steps(()) == "No build steps recorded for this run."


def test_task_graph_parser_rejects_bad_shape() -> None:
    try:
        parse_task_graph_list({"steps": "bad"})
    except PanelClientError:
        pass
    else:
        raise AssertionError("malformed task graph result must fail closed")
