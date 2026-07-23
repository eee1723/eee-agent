from houdini_side.runtime_panel.view_models import approval_result_card


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
