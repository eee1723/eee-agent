from __future__ import annotations

import pytest

from eee_agent.cli import build_parser


@pytest.mark.parametrize("removed", ["selftest", "prompt", "stdio"])
def test_legacy_cli_modes_are_not_registered(removed: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([removed])


def test_cli_keeps_only_versions_mode() -> None:
    assert build_parser().parse_args(["versions"]).mode == "versions"
