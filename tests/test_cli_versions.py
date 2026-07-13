import json

from eee_agent.cli import print_versions


def test_print_versions_emits_json(capsys) -> None:
    assert print_versions() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dependencies"]["langchain-anthropic"] == "1.4.8"
