import pytest

from eee_agent.core.ids import IdKind, new_id, require_id


def test_new_id_has_kind_prefix_and_uuid_payload() -> None:
    value = new_id(IdKind.SESSION)
    prefix, payload = value.split("_", 1)
    assert prefix == "ses"
    assert len(payload) == 32
    int(payload, 16)


def test_require_id_rejects_wrong_kind() -> None:
    with pytest.raises(ValueError, match="expected run_"):
        require_id("ses_0123456789abcdef0123456789abcdef", IdKind.RUN)


def test_require_id_rejects_non_hex_payload() -> None:
    with pytest.raises(ValueError, match="invalid artifact id"):
        require_id("art_not-hex", IdKind.ARTIFACT)
