from __future__ import annotations

from dataclasses import replace

import pytest

from eee_agent.changesets.contracts import (
    ChangeReceipt,
    ReceiptStatus,
)
from eee_agent.modeling.bootstrap import (
    BootstrapFinalizeError,
    derive_bootstrap_manifest,
)
from tests.modeling.test_compiler import NOW, WS, _compile_bootstrap


def _receipt(**overrides: object) -> ChangeReceipt:
    changeset = _compile_bootstrap().changeset
    values: dict[str, object] = {
        "change_id": changeset.change_id,
        "status": ReceiptStatus.APPLIED,
        "instance_id": changeset.scene_binding.instance_id,
        "scene_epoch": changeset.scene_binding.scene_epoch,
        "before_revision": changeset.base_revision,
        "after_revision": "b" * 64,
        "applied_op_ids": tuple(op.op_id for op in changeset.operations),
        "postcondition_results": (),
        "rollback_results": (),
        "scene_may_have_changed": False,
        "completed_at": NOW,
    }
    values.update(overrides)
    return ChangeReceipt(**values)  # type: ignore[arg-type]


def test_applied_bootstrap_derives_exact_manifest() -> None:
    changeset = _compile_bootstrap().changeset
    manifest = derive_bootstrap_manifest(changeset, _receipt())
    assert manifest.workspace_id == WS
    assert manifest.session_id == changeset.session_id
    assert manifest.created_by_run == changeset.run_id
    assert manifest.instance_id == changeset.scene_binding.instance_id
    assert [node.path for node in manifest.nodes] == [
        "/obj/eee_model",
        "/obj/eee_model/box1",
        "/obj/eee_model/xform1",
    ]
    assert manifest.roots == (manifest.nodes[0],)
    assert manifest.nodes[0].role == "root"
    assert manifest == derive_bootstrap_manifest(changeset, _receipt())


@pytest.mark.parametrize(
    "receipt",
    [
        _receipt(
            status=ReceiptStatus.ROLLED_BACK,
            applied_op_ids=(),
            scene_may_have_changed=False,
        ),
        _receipt(applied_op_ids=()),
        _receipt(instance_id="other_houdini"),
    ],
)
def test_manifest_derivation_rejects_non_applied_incomplete_or_mismatched_receipt(
    receipt: ChangeReceipt,
) -> None:
    with pytest.raises(BootstrapFinalizeError):
        derive_bootstrap_manifest(_compile_bootstrap().changeset, receipt)


def test_manifest_derivation_rejects_non_bootstrap_changeset() -> None:
    changeset = _compile_bootstrap().changeset
    with pytest.raises(BootstrapFinalizeError) as exc:
        derive_bootstrap_manifest(
            replace(changeset, workspace_id=WS),
            _receipt(),
        )
    assert exc.value.code == "modeling.bootstrap_changeset_invalid"

