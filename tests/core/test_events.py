from datetime import datetime, timezone

import pytest

from eee_agent.core.events import DomainEvent


def test_domain_event_serializes_utc_timestamp_and_payload() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": "deepseek-v4-pro", "tokens": 12},
        timestamp=datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
    )

    data = event.to_dict()

    assert data["event_type"] == "provider.model_completed"
    assert data["timestamp"] == "2026-07-13T04:00:00+00:00"
    assert data["payload"] == {"model": "deepseek-v4-pro", "tokens": 12}
    assert data["schema_version"] == 1


def test_domain_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainEvent.create(
            event_type="run.created",
            payload={},
            timestamp=datetime(2026, 7, 13, 4, 0),
        )


def test_domain_event_requires_namespaced_type() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        DomainEvent.create(event_type="created", payload={})
