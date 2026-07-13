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

@pytest.mark.parametrize(
    "path",
    [
        "C:/outside/report.json",
        "C:outside/report.json",
        ".",
        "./report.json",
        "runs/nested/../../report.json",
        "runs/\0/report.json",
        "runs//report.json",
        "runs/report.json/",
        "runs/./report.json",
        "",
    ],
)
def test_artifact_ref_rejects_noncanonical_or_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactRef(artifact_id=new_id(IdKind.ARTIFACT), relative_path=path, sha256="c" * 64, media_type="application/json", size_bytes=1)

def test_artifact_ref_requires_string_relative_path() -> None:
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactRef(artifact_id=new_id(IdKind.ARTIFACT), relative_path=42, sha256="c" * 64, media_type="application/json", size_bytes=1)  # type: ignore[arg-type]

def _make_artifact_ref(**overrides: object) -> ArtifactRef:
    values: dict[str, object] = {
        "artifact_id": new_id(IdKind.ARTIFACT),
        "relative_path": "runs/report.json",
        "sha256": "d" * 64,
        "media_type": "application/json",
        "size_bytes": 1,
    }
    values.update(overrides)
    return ArtifactRef(**values)  # type: ignore[arg-type]

@pytest.mark.parametrize(
    ("field", "value"),
    [("artifact_id", 42), ("sha256", 42), ("media_type", 42)],
)
def test_artifact_ref_requires_string_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _make_artifact_ref(**{field: value})

@pytest.mark.parametrize("size_bytes", [True, 1.5, -1, "1"])
def test_artifact_ref_rejects_invalid_size_bytes(size_bytes: object) -> None:
    with pytest.raises(ValueError, match="size_bytes"):
        _make_artifact_ref(size_bytes=size_bytes)

@pytest.mark.parametrize("schema_version", [True, False, 1.0, 0, -1, 2, "1"])
def test_artifact_ref_requires_supported_schema_version(schema_version: object) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _make_artifact_ref(schema_version=schema_version)

@pytest.mark.parametrize(
    "media_type",
    [
        "/",
        "text/",
        "text / plain",
        " text/plain",
        "text/plain ",
        "text/\nplain",
        "text/plain; charset=utf-8",
        "text//plain",
        "*/*",
        "text/*",
    ],
)
def test_artifact_ref_rejects_invalid_media_type(media_type: str) -> None:
    with pytest.raises(ValueError, match="media_type"):
        _make_artifact_ref(media_type=media_type)

@pytest.mark.parametrize(
    "media_type", ["application/json", "image/png", "application/vnd.api+json"]
)
def test_artifact_ref_accepts_concrete_media_types(media_type: str) -> None:
    assert _make_artifact_ref(media_type=media_type).media_type == media_type

@pytest.mark.parametrize(
    "device_name",
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{number}" for number in range(1, 10)]
    + [f"LPT{number}" for number in range(1, 10)],
)
@pytest.mark.parametrize("extension", ["", ".txt"])
def test_artifact_ref_rejects_windows_reserved_device_components(
    device_name: str, extension: str
) -> None:
    component = f"{device_name.lower()}{extension}"
    with pytest.raises(ValueError, match="artifact path"):
        _make_artifact_ref(relative_path=f"runs/{component}")

@pytest.mark.parametrize(
    "path",
    [
        "runs/file.txt:stream",
        "runs/name<report.json",
        "runs/name>report.json",
        'runs/name"report.json',
        "runs/name|report.json",
        "runs/name?report.json",
        "runs/name*report.json",
    ],
)
def test_artifact_ref_rejects_windows_invalid_component_characters(
    path: str,
) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        _make_artifact_ref(relative_path=path)

@pytest.mark.parametrize("code_point", range(0x20))
def test_artifact_ref_rejects_ascii_control_characters(code_point: int) -> None:
    path = f"runs/name{chr(code_point)}report.json"
    with pytest.raises(ValueError, match="artifact path"):
        _make_artifact_ref(relative_path=path)

@pytest.mark.parametrize(
    "path",
    [
        "runs/name ",
        "runs/name.",
        "runs/name /report.json",
        "runs/name./report.json",
    ],
)
def test_artifact_ref_rejects_components_ending_in_space_or_period(
    path: str,
) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        _make_artifact_ref(relative_path=path)

@pytest.mark.parametrize(
    "path",
    [
        "runs/My Report/report v1.final.json",
        "runs/vendor.example/package-1.0/artifact.tar.gz",
        "runs/com10/lpt0/console.json",
        "runs/AUXILIARY/COM1-data/LPT9_report.json",
    ],
)
def test_artifact_ref_accepts_windows_safe_components(path: str) -> None:
    assert _make_artifact_ref(relative_path=path).relative_path == path
