from eee_agent.core import versioning
from eee_agent.core.versioning import TRACKED_DISTRIBUTIONS, runtime_version_report


def test_runtime_version_report_contains_reproducibility_fields() -> None:
    report = runtime_version_report()
    assert report["eee_agent"] == "0.1.0"
    assert report["python"].startswith("3.11.")
    assert report["dependencies"]["deepagents"] == "0.6.12"
    assert report["dependencies"]["rpyc"] == "4.1.0"
    # Every tracked distribution appears as a key; runtime fields are present.
    assert set(report["dependencies"]) == set(TRACKED_DISTRIBUTIONS)
    assert report["python_executable"]
    assert report["platform"]


def test_runtime_version_report_handles_missing_distribution(monkeypatch) -> None:
    # A distribution name with no installed metadata must degrade to None, not
    # crash the whole report (matters on minimal provider-specific installs).
    monkeypatch.setattr(
        versioning,
        "TRACKED_DISTRIBUTIONS",
        TRACKED_DISTRIBUTIONS + ("nonexistent-dist-zzz",),
    )
    report = runtime_version_report()
    assert report["dependencies"]["nonexistent-dist-zzz"] is None
    # The other fields are still populated.
    assert report["eee_agent"] == "0.1.0"
    assert report["python"].startswith("3.11.")
