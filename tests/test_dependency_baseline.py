from importlib import metadata
from pathlib import Path

import pytest


EXPECTED_DIRECT_VERSIONS = {
    "deepagents": "0.6.12",
    "langchain": "1.3.13",
    "langchain-core": "1.4.9",
    "langchain-openai": "1.3.5",
    "langchain-anthropic": "1.4.8",
    "langgraph": "1.2.9",
    "rpyc": "4.1.0",
    "pyyaml": "6.0.3",
    "python-dotenv": "1.2.2",
}


def test_uv_lock_is_committed() -> None:
    assert Path("uv.lock").is_file()


@pytest.mark.parametrize(("distribution", "expected"), EXPECTED_DIRECT_VERSIONS.items())
def test_direct_dependency_version(distribution: str, expected: str) -> None:
    assert metadata.version(distribution) == expected
