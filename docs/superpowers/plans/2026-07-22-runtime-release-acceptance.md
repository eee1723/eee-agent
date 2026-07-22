# Runtime Release Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for Vision behavior changes, superpowers:systematic-debugging for every real-Houdini/provider failure, and superpowers:verification-before-completion before release-readiness claims. Track execution with checkbox (`- [ ]`) state.

**Goal:** Prove the accepted Runtime in offline, real Houdini 21.0.440, interactive panel, and real Vision-provider environments, producing a complete redacted release-candidate evidence package without tagging or publishing it.

**Architecture:** Stage B is four concentric gates: deterministic offline checks, disposable hython integration, interactive Houdini UI, and an explicitly configured provider-neutral Vision adapter. Production keeps Vision disabled unless `EEE_VISION_PROVIDER` is set. The adapter receives only verified ArtifactStore bytes; the existing `VisionRouter` remains the trust boundary and normalizes provider output into strict contracts.

**Tech Stack:** Python 3.11, Houdini/hython 21.0.440, PySide6 6.5.3 supplied by Houdini, LangChain provider adapters already locked by the project, pytest, PowerShell.

**Workspace:** `E:\eee-agent\.worktrees\runtime`, branch `feature/b-release-acceptance` created only after Stage A acceptance. External evidence root: `E:\eee-agent-local-acceptance\2026-07-22-stage-b`. Do not commit screenshots, HIP files, provider payloads, credentials, discovery tokens, or process logs.

**Approved spec:** `docs/superpowers/specs/2026-07-22-runtime-release-acceptance-design.md`

---

### Task 1: Establish the exact B candidate and evidence ledger

**Files:**

- Create: `docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md`
- Modify: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`
- Local only: `E:\eee-agent-local-acceptance\2026-07-22-stage-b\`

- [ ] **Step 1: Require accepted Stage A and create B branch**

```powershell
git status --porcelain=v1
git log -1 --oneline
rg -n "Decision: PASS|Stage A: PASS" docs/superpowers/reviews/2026-07-22-stage-a-acceptance.md
git switch -c feature/b-release-acceptance
```

Expected: clean Stage A tip, explicit PASS evidence, new B branch at that exact commit. If any condition fails, stop B.

- [ ] **Step 2: Create a redacted evidence directory outside Git**

Resolve the directory and require it to start with `E:\eee-agent-local-acceptance\`. Create subdirectories `hython`, `gui`, `provider`, and `logs`:

```powershell
$evidenceRoot = 'E:\eee-agent-local-acceptance\2026-07-22-stage-b'
New-Item -ItemType Directory -Force -Path $evidenceRoot | Out-Null
foreach ($name in @('hython', 'gui', 'provider', 'logs')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $evidenceRoot $name) | Out-Null
}
$resolvedEvidence = (Resolve-Path -LiteralPath $evidenceRoot).Path
if (-not $resolvedEvidence.StartsWith('E:\eee-agent-local-acceptance\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Evidence target escaped the approved root: $resolvedEvidence"
}
```

Add that absolute root to the committed acceptance file only as `[machine-local acceptance root]`; do not commit the user-specific path.

- [ ] **Step 3: Seed the acceptance ledger**

The committed Markdown uses a row per gate with columns: ID, exact commit, environment, command/action, expected, result (`PASS`/`FAIL`/`NOT RUN`), redacted evidence digest, finding ID. Start every result as `NOT RUN`. A missing credential/license is `NOT RUN` or external `FAIL`, never PASS.

---

### Task 2: Implement explicit, provider-neutral Vision configuration

**Files:**

- Modify: `.env.example`
- Modify: `eee_agent/config.py`
- Create: `eee_agent/vision/provider.py`
- Modify: `eee_agent/vision/__init__.py`
- Modify: `eee_agent/runtime/__main__.py:183-194`
- Modify: `tests/test_config.py` or the existing configuration test module
- Create: `tests/runtime/test_vision_provider.py`
- Modify: `tests/runtime/test_runtime_cli.py`

- [ ] **Step 1: Write RED configuration tests**

Add a frozen `VisionConfig` and test these cases:

```python
def test_vision_config_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EEE_VISION_PROVIDER", raising=False)
    assert vision_config() is None


def test_vision_config_reuses_supported_provider_registry(monkeypatch) -> None:
    monkeypatch.setenv("EEE_VISION_PROVIDER", "openai")
    monkeypatch.setenv("EEE_VISION_MODEL", "gpt-4.1")
    config = vision_config()
    assert config is not None
    assert config.provider == "openai"
    assert config.model == "gpt-4.1"


def test_vision_config_rejects_implicit_or_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("EEE_VISION_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="EEE_VISION_PROVIDER"):
        vision_config()
```

Also test bounded positive `EEE_VISION_MAX_IMAGE_BYTES` (default 8 MiB, maximum 16 MiB) and `EEE_VISION_TIMEOUT_SECONDS` (default 30.0, range 0.1..120.0). Vision must not silently inherit `EEE_LLM_PROVIDER`; it is enabled only by its own provider variable.

- [ ] **Step 2: Implement configuration using the existing provider identifiers**

Add to `eee_agent/config.py`:

```python
@dataclass(frozen=True)
class VisionConfig:
    provider: str
    model: str
    max_image_bytes: int
    timeout_seconds: float


def vision_config() -> VisionConfig | None:
    raw = (os.getenv("EEE_VISION_PROVIDER") or "").strip().lower()
    if not raw:
        return None
    defaults = {"anthropic": "claude-sonnet-5", "openai": "gpt-4.1"}
    if raw not in defaults:
        raise ValueError(f"unknown EEE_VISION_PROVIDER: {raw!r}")
    model = (os.getenv("EEE_VISION_MODEL") or defaults[raw]).strip()
    maximum = int(os.getenv("EEE_VISION_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
    timeout = float(os.getenv("EEE_VISION_TIMEOUT_SECONDS", "30"))
    if not model or not 1 <= maximum <= 16_777_216 or not 0.1 <= timeout <= 120.0:
        raise ValueError("Vision configuration is invalid")
    return VisionConfig(raw, model, maximum, timeout)
```

In implementation, split integer/float parse failures into stable `ValueError` messages without echoing environment contents.

- [ ] **Step 3: Write RED adapter tests against a fake chat model**

The adapter contract tests must prove:

- capability exposes only `image/png`, `image/jpeg`, `image/webp` and configured max bytes;
- `evaluate()` builds one bounded instruction and one data URL from the supplied bytes;
- it never opens `request.artifact.relative_path`;
- it returns a plain mapping for `VisionRouter` normalization;
- empty, non-JSON, fenced JSON, oversized, or non-mapping responses are rejected;
- provider/model objects are injected so offline tests never use a network or credentials.

Expected output schema sent to the model:

```json
{
  "summary": "bounded text",
  "observations": ["bounded observation"],
  "confidence": 0.0,
  "advisory_passed": false
}
```

- [ ] **Step 4: Implement `LangChainVisionProvider` as an outer adapter**

The production class has no Houdini imports and accepts a `BaseChatModel`, provider id, and byte limit. Its `evaluate()` uses `HumanMessage` with a text block and an `image_url` data URL derived from the already verified `image_bytes`. Encode in memory; do not persist base64 or raw response. Parse a single JSON object, permitting one outer Markdown JSON fence but no surrounding prose, and return the mapping unchanged so `NormalizedVisualReport.from_dict()` remains authoritative.

The adapter prompt must state the exact four fields, forbid extra keys, require evidence limited to the supplied image, and include the bounded `request.instruction`. Cap extracted model text before JSON parsing (for example 16 KiB). Never log the content.

- [ ] **Step 5: Build the model through the existing provider registry**

Add a factory in `eee_agent/vision/provider.py`:

```python
def build_vision_provider(config: VisionConfig) -> LangChainVisionProvider:
    llm = LlmConfig(
        provider=config.provider,
        model=config.model,
        thinking_enabled=False,
        effort=None,
        max_output_tokens=2048,
    )
    resolved = resolve_model(llm)
    return LangChainVisionProvider(
        resolved.model,
        provider_id=config.provider,
        max_image_bytes=config.max_image_bytes,
    )
```

Do not import provider SDKs directly. This preserves existing credential resolution and provider errors.

- [ ] **Step 6: Wire explicit production configuration**

Replace `_vision_provider()` with:

```python
def _vision_provider() -> VisionProvider | None:
    from eee_agent.config import vision_config
    from eee_agent.vision.provider import build_vision_provider

    config = vision_config()
    return None if config is None else build_vision_provider(config)
```

Pass `config.timeout_seconds` to the existing `RuntimeService`/`VisionRouter` seam if it is not already possible. If that seam currently owns a fixed timeout, add the smallest typed `vision_timeout_seconds` argument to `RuntimeService.open`; do not put timeout policy in the Houdini panel.

- [ ] **Step 7: Document only variable names and safe examples**

Add disabled examples to `.env.example`:

```dotenv
# EEE_VISION_PROVIDER=openai
# EEE_VISION_MODEL=gpt-4.1
# EEE_VISION_MAX_IMAGE_BYTES=8388608
# EEE_VISION_TIMEOUT_SECONDS=30
```

Do not add API key values or claim every text-only model supports images.

- [ ] **Step 8: Run focused gates and commit**

```powershell
uv run --frozen --extra eval pytest -q `
  tests/runtime/test_vision_provider.py `
  tests/runtime/test_vision_router.py `
  tests/runtime/test_vision_contracts.py `
  tests/runtime/test_runtime_cli.py
uv run --frozen ruff check eee_agent/config.py eee_agent/vision eee_agent/runtime/__main__.py tests/runtime/test_vision_provider.py tests/runtime/test_runtime_cli.py
git add .env.example eee_agent/config.py eee_agent/vision eee_agent/runtime/__main__.py tests
git diff --cached --check
git commit -m "feat: configure provider-neutral visual evaluation"
```

---

### Task 3: Give `FAILED` a real and tested Vision meaning

**Files:**

- Modify: `eee_agent/vision/router.py`
- Modify: `eee_agent/vision/contracts.py` only if invariant clarification is needed
- Modify: `eee_agent/vision/evaluation.py` only if current invariants reject a report-less FAILED outcome
- Modify: `tests/runtime/test_vision_router.py`
- Modify: `tests/runtime/test_vision_contracts.py`
- Modify: `tests/runtime/test_vision_delivery.py`
- Modify: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md` (RN-006)

- [ ] **Step 1: Lock the state boundary with RED tests**

Expected classification:

| Boundary | Status | Reason |
|---|---|---|
| no configured provider | `UNAVAILABLE` | no evaluation can start |
| capability unavailable/invalid/raises | `UNAVAILABLE` | provider cannot accept work |
| unsupported/missing/corrupt Artifact | `UNAVAILABLE` | no provider evaluation starts |
| provider evaluation timeout | `FAILED` | accepted evaluation did not finish |
| provider evaluation exception | `FAILED` | attempted evaluation failed |
| provider response invalid | `FAILED` | attempted evaluation produced unusable evidence |
| strict normalized report | `COMPLETED` | advisory result exists |
| explicit user waiver | `WAIVED` | evaluation intentionally skipped |

For every FAILED case assert `accepted is False`, deterministic validation is preserved in the decision, no report exists, and a bounded safe reason is durable. Deterministic invalidity always remains not accepted.

- [ ] **Step 2: Add a typed failed outcome**

If `VisionUnavailable` cannot carry FAILED by design, add a separate strict contract:

```python
@dataclass(frozen=True, slots=True)
class VisionFailure(_StrictContract):
    status: VisionStatus
    reason_code: str
    message: str

    def __post_init__(self) -> None:
        if self.status is not VisionStatus.FAILED:
            raise ValueError("failure status must be failed")
        _text(self.reason_code, "reason_code", maximum=64)
        _text(self.message, "message", maximum=512)
```

Extend `VisionOutcome` with `failure: VisionFailure | None` and enforce exactly one of report/unavailable/failure for the corresponding status. Add `_failed(...)` next to `_unavailable(...)`. Route only post-capability evaluation timeout/exception/invalid response through `_failed`.

- [ ] **Step 3: Verify delivery/event projection keeps the distinction**

Run and update `test_vision_delivery.py` so emitted status/reason is `failed` for attempted-provider failures and `unavailable` for pre-invocation gaps. Panel view-model tests must map both to warn/error according to product semantics, but neither may look accepted.

- [ ] **Step 4: Commit the semantic correction**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_vision_contracts.py tests/runtime/test_vision_router.py tests/runtime/test_vision_delivery.py tests/panel/test_runtime_panel_view_models.py
git add eee_agent/vision tests/runtime/test_vision_contracts.py tests/runtime/test_vision_router.py tests/runtime/test_vision_delivery.py tests/panel/test_runtime_panel_view_models.py docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "fix: distinguish failed visual evaluation from unavailable"
```

---

### Task 4: Run deterministic offline and HFS gates

**Files:**

- Update evidence rows only; product code changes require a new registered finding and RED test.

- [ ] **Step 1: Run the complete offline candidate gate**

```powershell
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision
python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
```

Expected: all pass, zero unexpected warnings. Record count/duration/skips.

- [ ] **Step 2: Resolve Houdini 21.0.440 without guessing**

Check `$env:HFS`, then the installed-package path already used on this machine. Require `bin\hython.exe` and verify:

```powershell
& "$hfs\bin\hython.exe" -c "import hou,sys; print(hou.applicationVersionString()); print(sys.version)"
```

Expected: Houdini `21.0.440`, Python `3.11.x`. A different build is recorded and reviewed before acceptance.

- [ ] **Step 3: Run opt-in HFS contracts**

```powershell
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
$env:HFS=$hfs
uv run --frozen --extra eval pytest -q -m houdini_kb
```

Expected current baseline: 11 pass. Always remove temporary process environment variables after evidence capture.

- [ ] **Step 4: Run disposable real-hython smokes sequentially**

Use a fresh machine-local state directory for each script:

```powershell
$bridgeState = Join-Path $env:TEMP ("eee-bridge-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $bridgeState | Out-Null
& "$hfs\bin\hython.exe" -u tests\runtime\houdini_bridge_smoke.py --state-dir $bridgeState --host 127.0.0.1 --port 0
& "$hfs\bin\hython.exe" -u tests\runtime\changeset_houdini_smoke.py
& "$hfs\bin\hython.exe" -u tests\runtime\capture_houdini_smoke.py
& "$hfs\bin\hython.exe" -u tests\runtime\sensitivity_houdini_smoke.py
& "$hfs\bin\hython.exe" -u tests\modeling\bootstrap_houdini_smoke.py
& "$hfs\bin\hython.exe" -u tests\modeling\golden_cases_houdini_smoke.py
```

Expected: every script reports all checks passed, restores/deletes owned temporary nodes, and leaves no saved HIP. If a command's current CLI differs, read its module docstring and record the exact actual command; do not invent flags.

- [ ] **Step 5: Triage failures before modifying code**

For each failure capture script, step, Houdini build, bounded error, scene cleanup result, and minimal reproduction. Register it. Only a confirmed in-scope code defect may open a new TDD patch; license/configuration failures remain external gates.

---

### Task 5: Execute the interactive Houdini panel checklist

**Files:**

- Create: `docs/superpowers/reviews/2026-07-22-stage-b-gui-checklist.md`
- Local only: screenshots in the Stage B evidence directory

- [ ] **Step 1: Deploy the exact candidate without changing the package path**

Verify the installed JSON still targets `E:/eee-agent/.worktrees/runtime`, the worktree is on the B candidate, and no unsaved user scene will be lost. Restart only the Runtime process spawned/owned by the panel. Do not kill a user-owned Runtime or Houdini process.

- [ ] **Step 2: Verify New Session behavior and last-night visual language**

Record PASS/FAIL/NOT RUN for: graphite/iron base, cyan focus/accent, amber approval-only semantics, modal focus, mouse, Escape, no accidental Return default, Chinese IME composition/confirmation, manual title, placeholder auto-title, no duplicate sidebar entries.

- [ ] **Step 3: Verify responsive three-pane structure**

Test wide, medium, narrow, docked, floating, resized, tab switching, and long content. Confirm Run/Workspace/Artifacts remain structured widgets, failure evidence is visible, no dictionary repr or `key:value` wall becomes the primary UI, and semantic errors do not use approval amber.

- [ ] **Step 4: Verify the full conversation lifecycle**

Exercise: create first Session automatically, stream text/thinking in place, switch Sessions during/after Runs, reconnect, Completed, Cancelled, Failed-before-output, Failed-after-partial-output, Stop, Force Stop. Confirm no duplicates, blank terminal cards, mixed-session evidence, or accidental IME submit.

- [ ] **Step 5: Verify approval/Apply/recovery**

Exercise MODEL -> REVIEW -> APPROVE -> APPLY -> RESULT plus reject, expiry, stale scene, forced rollback/recovery. Confirm exact digest binding, single Apply, durable receipt, structured apply error, and no success presentation after uncertain recovery.

- [ ] **Step 6: Verify lifecycle ownership**

Test automatic backend start, stale discovery rejection, reconnect, Runtime restart, panel close/reopen, Houdini close, and subprocess reap. The panel may terminate only a backend it spawned and still owns.

- [ ] **Step 7: Commit the completed checklist state**

Every item must remain explicit. Screenshots stay local; the Markdown stores only redacted digest, observed result, commit, Houdini build, and finding ID. User-observed interactive items require the user's confirmation; Codex must not infer PASS from code inspection.

---

### Task 6: Run the real provider + real Artifact Vision journey

**Files:**

- Modify: `tests/runtime/provider_journey.py`
- Modify: `tests/runtime/runtime_mvp_provider_e2e.py` if evidence schema expands
- Modify: `tests/runtime/test_runtime_mvp_e2e.py`
- Create or modify: `tests/runtime/vision_provider_journey.py` if separation keeps the existing MVP harness stable
- Update: Stage B acceptance evidence

- [ ] **Step 1: Extend the strict evidence schema with Vision facts**

Add only bounded normalized fields: `vision_status`, `vision_accepted`, `vision_reason_code`, `vision_artifact_digest_match`. Never add prompt, model text, image bytes/base64, credential, raw response, or full local path.

- [ ] **Step 2: Write RED harness tests with a subprocess stub**

Update offline harness tests to accept only the expanded exact-key schema, reject extra/unbounded fields, reject `accepted=true` when deterministic validation failed, and report absent explicit Vision configuration as NOT RUN rather than fake pass.

- [ ] **Step 3: Wire the journey to production `_vision_provider()`**

Replace the current hardcoded `"vision_provider": None` in `_service_wiring()` with the same factory used by production. After the captured Artifact becomes available, require the durable Vision event/outcome to reference that exact artifact ID and SHA-256. Do not re-read a user path or capture a second image for the provider.

- [ ] **Step 4: Run the opt-in real journey with bounded timeout**

Set variables in the process only; never print them:

```powershell
$env:EEE_RUN_RUNTIME_MVP_PROVIDER_E2E='true'
$env:EEE_RUNTIME_MVP_PROVIDER_COMMAND='python tests\runtime\provider_journey.py'
$env:EEE_RUNTIME_MVP_HFS="$hfs\bin\hython.exe"
$env:EEE_RUNTIME_MVP_HIP_PATH=Join-Path $evidenceRoot 'provider\journey.hip'
$env:EEE_RUNTIME_MVP_EVIDENCE_PATH=Join-Path $evidenceRoot 'provider\journey-evidence.json'
uv run --frozen --extra eval pytest -q tests\runtime\runtime_mvp_provider_e2e.py -s
```

Use the configured provider/model already approved for this machine. Allow the existing 600-second Run budget and the configured Vision timeout. Do not launch a second provider journey while one is pending. A transient external failure is retried only after recording the first bounded result.

- [ ] **Step 5: Verify evidence and cleanup**

Require: proposal digest valid, approval event present, receipt applied/already-applied, deterministic validation passed, artifact available, exact Artifact digest match, Vision status completed, normalized decision present, replay sequence survived restart, scene cleanup passed. Hash the evidence file, then inspect it for key names and bounded sizes without printing secrets.

- [ ] **Step 6: Commit harness and evidence metadata**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_runtime_mvp_e2e.py tests/runtime/test_vision_provider.py tests/runtime/test_vision_router.py tests/runtime/test_vision_delivery.py
git add tests/runtime docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "test: verify real visual provider delivery journey"
```

---

### Task 7: Decide release-candidate readiness

**Files:**

- Finalize: `docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md`
- Finalize: `docs/superpowers/reviews/2026-07-22-stage-b-gui-checklist.md`
- Update: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`

- [ ] **Step 1: Re-run the complete static/offline gate on final B HEAD**

```powershell
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision
python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
git diff --check main...HEAD
git status --short --branch
```

- [ ] **Step 2: Perform Codex final review**

Review provider byte provenance, secret redaction, no SDK import in Houdini-side code, FAILED/UNAVAILABLE truthfulness, advisory-only semantics, process cleanup, GUI evidence completeness, and all blocker/high findings. Add newly discovered defects to the register even if not in the original plan.

- [ ] **Step 3: Record PASS or NOT READY without ambiguity**

PASS requires every offline/HFS/hython gate, every required GUI item, and the real provider journey to pass on the same committed candidate, with no unresolved blocker/high finding. Otherwise record NOT READY and the exact blocking IDs. Do not create/push an RC tag, merge, or delete branches without separate user authorization.

- [ ] **Step 4: Commit final evidence**

```powershell
git add docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md `
  docs/superpowers/reviews/2026-07-22-stage-b-gui-checklist.md `
  docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "docs: record runtime release acceptance decision"
```
