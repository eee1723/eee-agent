"""Register the EEE Agent Houdini package and docked Python Panel.

The package makes the Secure Bridge host, Runtime observer, and legacy
``start_rpc`` / ``chat_panel`` rollback modules importable.

Run once (from a shell or Houdini):
    hython Z:\\EEE_Project\\EEEProceduralModeling\\houdini_side\\install_menu.py
then restart Houdini. An "EEE Agent" menu appears in the menu bar.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def get_root() -> Path:
    return Path(__file__).resolve().parent.parent  # houdini_side -> repo root


def get_packages_dir() -> Path | None:
    candidates: list[Path] = []
    try:
        import hou  # running under hython / Houdini
        prefs = hou.getenv("HOUDINI_USER_PREF_DIR") or hou.homeHoudiniDirectory()
        candidates.append(Path(prefs) / "packages")
    except ImportError:
        pass
    env_prefs = os.environ.get("HOUDINI_USER_PREF_DIR")
    if env_prefs:
        candidates.append(Path(env_prefs) / "packages")
    # common Windows default locations
    home = Path.home()
    for v in ("houdini21.0", "houdini21.5", "houdini20.5"):
        candidates.append(home / "Documents" / v / "packages")

    for d in candidates:
        if d.exists() and d.is_dir():
            return d
    # fall back to creating the first plausible one
    return candidates[0] if candidates else None


def install() -> None:
    root = get_root()
    packages_dir = get_packages_dir()
    if packages_dir is None:
        print("ERROR: could not find a Houdini packages directory.")
        print("Set HOUDINI_USER_PREF_DIR or run under hython inside Houdini.")
        sys.exit(1)

    packages_dir.mkdir(parents=True, exist_ok=True)
    package_file = packages_dir / "eee_agent.json"
    root_fwd = str(root).replace("\\", "/")

    # Houdini package format (same shape as Edini's edini.json):
    #  - path: adds $EEE_PATH to HOUDINI_PATH -> MainMenuCommon.xml at root auto-loads
    #  - houdini.python3.11libs: makes Houdini-side entry modules importable
    with open(package_file, "w", encoding="utf-8") as f:
        json.dump({
            "env": [
                {"EEE_PATH": root_fwd},
                # Make Houdini-side entry modules importable everywhere
                # (belt-and-suspenders alongside houdini.python3.11libs).
                {"PYTHONPATH": "$EEE_PATH/houdini_side"},
            ],
            "path": "$EEE_PATH",
            "houdini": {"python3.11libs": "$EEE_PATH/houdini_side"},
        }, f, indent=2)

    print(f"EEE Agent package installed.")
    print(f"  package file: {package_file}")
    print(f"  project root: {root_fwd}")
    print(f"  menu xml:     {root_fwd}/MainMenuCommon.xml")
    print(f"  python panel: {root_fwd}/python_panels/EEEAgentRuntime.pypanel")
    print()
    print("Next: restart Houdini -> 'EEE Agent' menu appears in the menu bar.")


if __name__ == "__main__":
    install()
