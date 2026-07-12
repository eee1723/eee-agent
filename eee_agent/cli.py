"""Command-line entry points for the eee_agent.

Modes:
  python -m eee_agent.cli selftest
      Exercise the RPC bridge + tools directly (no LLM, no API key). Creates a
      box in Houdini, reads stats (expect 8 points / 6 prims), exports an .obj.
      This is the Phase-1 end-to-end bridge verification.

  python -m eee_agent.cli prompt "build a 3-storey house"
      One-shot: build the agent and run a single user turn. Prints the final
      answer + which tools ran. Needs an LLM API key + Houdini RPC running.

  python -m eee_agent.cli stdio
      JSON-lines over stdin/stdout for the Houdini PySide6 panel (Phase 3).
      Reads {"type":"user","text":...} lines; emits {"type":"token",...},
      {"type":"tool",...}, {"type":"done"}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from eee_agent.config import repo_root


# --------------------------------------------------------------------------- #
# selftest: bridge + tools, no LLM
# --------------------------------------------------------------------------- #
def selftest() -> int:
    from eee_agent.tools import inspect, nodes, scene

    def step(label, fn):
        print(f"\n=== {label} ===", flush=True)
        result = fn()
        print(json.dumps(result, indent=2, default=str), flush=True)
        return result

    status = step("hou_status", lambda: scene.hou_status.invoke({}))
    if not status.get("connected"):
        print("\n[FAIL] Bridge not connected. Start houdini_side/start_rpc.py in "
              "Houdini first.", file=sys.stderr)
        return 1

    step("scene_reset /obj", lambda: scene.scene_reset.invoke({"scope": "/obj"}))
    geo = step("create geo", lambda: nodes.create_node.invoke(
        {"parent_path": "/obj", "node_type": "geo", "name": "selftest_geo"}))
    geo_path = geo["node"]["path"]
    box = step("create box", lambda: nodes.create_node.invoke(
        {"parent_path": geo_path, "node_type": "box", "name": "box",
         "connect_from": None}))
    box_path = box["node"]["path"]
    step("set size 2x2x2", lambda: nodes.set_parms.invoke(
        {"node_path": box_path, "parms": {"size": [2, 2, 2]}}))
    stats = step("geometry_stats", lambda: inspect.geometry_stats.invoke(
        {"node_path": box_path}))

    ok = stats.get("points") == 8 and stats.get("prims") == 6
    print(f"\n[{'PASS' if ok else 'FAIL'}] box stats: "
          f"points={stats.get('points')} prims={stats.get('prims')} "
          f"(expected 8 / 6)", flush=True)

    out_dir = os.path.join(repo_root(), "output")
    os.makedirs(out_dir, exist_ok=True)
    out_obj = os.path.join(out_dir, "selftest_box.obj")
    step("export obj", lambda: inspect.export_geometry.invoke(
        {"node_path": box_path, "file_path": out_obj}))
    print(f"\nExported: {out_obj}  exists={os.path.exists(out_obj)}", flush=True)
    return 0 if ok else 2


# --------------------------------------------------------------------------- #
# prompt: one-shot agent run
# --------------------------------------------------------------------------- #
def run_prompt(text: str) -> int:
    from eee_agent.app import build_agent
    from eee_agent.config import recursion_limit

    try:
        agent = build_agent()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] could not build agent: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"recursion_limit": recursion_limit()},
        )
    except Exception as e:  # noqa: BLE001
        ename = type(e).__name__
        if "Recursion" in ename:
            print(f"\n[STOPPED] hit step limit ({recursion_limit()}). The agent "
                  "may still have produced output — check Houdini and the export "
                  "path before retrying.", file=sys.stderr)
        else:
            print(f"\n[ERROR] {ename}: {e}", file=sys.stderr)
        return 3

    messages = result.get("messages", [])
    tool_calls = sum(
        1 for m in messages if getattr(m, "type", "") == "tool"
        or m.__class__.__name__ == "ToolMessage"
    )
    final = messages[-1] if messages else None
    print("\n=== FINAL ANSWER ===")
    print(getattr(final, "content", str(final)))
    print(f"\n(tools executed: {tool_calls})")
    return 0


# --------------------------------------------------------------------------- #
# stdio: JSON-lines for the PySide6 panel
# --------------------------------------------------------------------------- #
async def _astream(agent, text: str):
    """Yield (kind, payload) events from a single agent turn."""
    from eee_agent.config import recursion_limit

    try:
        async for chunk, metadata in agent.astream(
            {"messages": [{"role": "user", "content": text}]},
            stream_mode="messages",
            config={"recursion_limit": recursion_limit()},
        ):
            cls = chunk.__class__.__name__
            content = getattr(chunk, "content", "")
            if cls == "AIMessageChunk" and content:
                yield ("token", {"text": content})
            elif cls == "ToolMessage":
                yield ("tool", {"name": getattr(chunk, "name", "tool"),
                                 "content": str(content)[:500]})
        yield ("done", {})
    except Exception as e:  # noqa: BLE001
        yield ("error", {"text": f"{type(e).__name__}: {e}"})


async def stdio() -> int:
    from eee_agent.app import build_agent

    agent = build_agent()
    loop = asyncio.get_event_loop()
    print(json.dumps({"type": "ready"}), flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"type": "error", "text": "malformed JSON line"}),
                  flush=True)
            continue
        if msg.get("type") != "user":
            continue
        async for kind, payload in _astream(agent, msg.get("text", "")):
            print(json.dumps({"type": kind, **payload}), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="eee_agent")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("selftest", help="verify bridge + tools without an LLM")
    p_prompt = sub.add_parser("prompt", help="one-shot agent run")
    p_prompt.add_argument("text")
    sub.add_parser("stdio", help="JSON-lines for the Houdini panel")
    args = ap.parse_args()

    if args.mode == "selftest":
        return selftest()
    if args.mode == "prompt":
        return run_prompt(args.text)
    if args.mode == "stdio":
        return asyncio.run(stdio())
    return 1


if __name__ == "__main__":
    sys.exit(main())
