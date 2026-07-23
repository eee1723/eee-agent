from houdini_side.runtime_panel.view_models import (
    approval_result_card,
    recover_result_card,
)


def test_approval_result_card_distinguishes_recovery_block() -> None:
    item = approval_result_card(
        True,
        blocked_recovery=True,
        blocker_ids=("chg_" + "a" * 32,),
    )
    assert item.title == "Approved — recovery required"
    assert "approved" in item.body.lower()
    assert "recovery" in item.body.lower()
    assert "chg_" + "a" * 32 in item.body


def test_recover_result_card_reflects_outcome() -> None:
    cid = "chg_" + "b" * 32
    ok = recover_result_card(cid, recovered=True)
    assert ok.title == "Recovered"
    assert ok.tone == "ok"

    refused = recover_result_card(cid, recovered=False)
    assert refused.title == "Recovery refused"
    assert refused.tone == "warn"

    pending = recover_result_card(cid, recovered=False, pending=True)
    assert pending.title == "Recovery pending"
    assert pending.tone == "warn"
