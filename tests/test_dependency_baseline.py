from importlib import metadata
from pathlib import Path
import re
import tomllib

import pytest


EXPECTED_DIRECT_VERSIONS = {
    "deepagents": "0.6.12",
    "langchain": "1.3.13",
    "langchain-core": "1.4.9",
    "langchain-openai": "1.3.5",
    "langchain-anthropic": "1.4.8",
    "langgraph": "1.2.9",
    "pyyaml": "6.0.3",
    "python-dotenv": "1.2.2",
    "aiosqlite": "0.22.1",
    "langgraph-checkpoint-sqlite": "3.1.0",
    "websockets": "15.0.1",
}

ROOT = Path(__file__).resolve().parents[1]


def _project_dependency_names() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    return {
        re.split(r"[<>=!~\[]", dependency, maxsplit=1)[0].strip().lower().replace("_", "-")
        for dependency in dependencies
    }


def _locked_package_names() -> set[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"].lower().replace("_", "-") for package in lock["package"]}


def test_uv_lock_is_committed() -> None:
    assert (ROOT / "uv.lock").is_file()


def test_legacy_rpyc_is_not_a_direct_dependency() -> None:
    forbidden = {"rpyc", "plumbum"}
    assert not forbidden & _project_dependency_names()
    assert not forbidden & _locked_package_names()


@pytest.mark.parametrize(("distribution", "expected"), EXPECTED_DIRECT_VERSIONS.items())
def test_direct_dependency_version(distribution: str, expected: str) -> None:
    assert metadata.version(distribution) == expected
