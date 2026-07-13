import pytest
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.ids import IdKind, new_id

def test_artifact_ref_serializes_stable_fields() -> None:
    ref = ArtifactRef(artifact_id=new_id(IdKind.ARTIFACT), relative_path="runs/run_1/report.json", sha256="a" * 64, media_type="application/json", size_bytes=42)
    data = ref.to_dict()
    assert data["schema_version"] == 1
    assert data["relative_path"] == "runs/run_1/report.json"
    assert data["sha256"] == "a" * 64

@pytest.mark.parametrize("path", ["/absolute/report.json", "../escape.json", r"runs\\bad.json"])
def test_artifact_ref_rejects_unsafe_relative_path(path: str) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactRef(artifact_id=new_id(IdKind.ARTIFACT), relative_path=path, sha256="b" * 64, media_type="application/json", size_bytes=1)

def test_artifact_ref_rejects_invalid_hash() -> None:
    with pytest.raises(ValueError, match="sha256"):
        ArtifactRef(artifact_id=new_id(IdKind.ARTIFACT), relative_path="report.json", sha256="not-a-hash", media_type="application/json", size_bytes=1)
