# Runtime Stability and Failure Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development while implementing each behavior change, then superpowers:verification-before-completion before every completion claim. Execute tasks in order and keep checkbox (`- [ ]`) state current.

**Goal:** Make the repository warning-clean and guarantee that Completed, Cancelled, and Failed Runs settle into explicit, bounded conversation and Inspector UI instead of an empty assistant card.

**Architecture:** Keep the Runtime protocol and state projection unchanged because `failure_json` is already durable and validated. Add one Qt-free `FailureView` normalizer and one pure terminal-card selector in `view_models.py`; make both history replay and live rendering call that selector; let `inspector.py` render the normalized failure only. Repair the two confirmed test-gate bugs at their source.

**Tech Stack:** Python 3.11, pytest 9, Ruff, Mypy, aiosqlite, PySide6 supplied only by Houdini, existing AST/source tests for Qt-dependent code.

**Workspace:** `E:\eee-agent\.worktrees\runtime`, branch `feature/a-stability`. All model implementation calls use exact model `glm-5.2[1m]` through Claude Code; Codex reviews every diff and runs the listed gates before committing. The implementation worker may not commit, push, merge, rebase, tag, edit `.env`, or alter worktrees.

**Approved spec:** `docs/superpowers/specs/2026-07-22-runtime-stability-design.md`

---

### Task 1: Restore strict warning and lint gates

**Files:**

- Modify: `tests/panel/test_main_window_history_behavior.py:15-17`
- Modify: `tests/runtime/test_runs.py:1390-1404`
- Modify: `pyproject.toml:[tool.pytest.ini_options]`
- Modify: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md` (RN-001, RN-002 evidence)

- [ ] **Step 1: Reproduce both failures before editing**

```powershell
uv run --frozen ruff check tests/panel/test_main_window_history_behavior.py
uv run --frozen --extra eval pytest -q `
  tests/runtime/test_runs.py::test_d2_update_todos_rejects_non_list `
  tests/runtime/test_runs.py::test_d2_update_todos_filters_non_mapping_items `
  -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Expected RED:

- Ruff reports `F401 pytest imported but unused` at line 16;
- the two-test sequence fails because the discarded `RuntimeDatabase` leaves an aiosqlite worker thread that reports into a closed event loop.

- [ ] **Step 2: Fix the resource lifecycle and unused import only**

Delete `import pytest` from the history behavior test only if Task 3 does not introduce a use for it. In `test_d2_update_todos_rejects_non_list`, retain the database and close it exactly like adjacent repository tests:

```python
async def scenario() -> None:
    db, sessions, runs = await _open(db_path)
    try:
        session = await sessions.create("A")
        run = await runs.create_and_acquire(
            session.session_id, "inspect", {"model": "fake"}
        )
        with pytest.raises(TypeError):
            await runs.update_todos(run.run_id, "not a list")  # type: ignore[arg-type]
    finally:
        await db.close()
```

Do not modify production database shutdown: the failing test is the confirmed owner of the leaked connection.

- [ ] **Step 3: Make unhandled thread warnings a persistent test failure**

Add the narrow rule to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
filterwarnings = [
    "error::pytest.PytestUnhandledThreadExceptionWarning",
]
```

Do not promote every third-party deprecation to an error in this slice.

- [ ] **Step 4: Prove GREEN at focused and suite scope**

```powershell
uv run --frozen ruff check tests/panel/test_main_window_history_behavior.py
uv run --frozen --extra eval pytest -q `
  tests/runtime/test_runs.py::test_d2_update_todos_rejects_non_list `
  tests/runtime/test_runs.py::test_d2_update_todos_filters_non_mapping_items
uv run --frozen --extra eval pytest -q tests/runtime/test_runs.py
```

Expected: all commands exit 0 with no unhandled-thread warning.

- [ ] **Step 5: Review and commit the gate repair**

Codex checks that only the test resource owner and narrow warning configuration changed, then:

```powershell
git add pyproject.toml tests/panel/test_main_window_history_behavior.py tests/runtime/test_runs.py `
  docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "test: close runtime database and enforce thread warning gate"
```

---

### Task 2: Add strict failure normalization and terminal-card selection

**Files:**

- Modify: `houdini_side/runtime_panel/view_models.py`
- Modify: `tests/panel/test_runtime_panel_view_models.py`

- [ ] **Step 1: Write RED tests for failure normalization**

Add tests covering valid retryable/non-retryable errors, malformed fields, generic fallback, bounds, stale failure on a non-Failed Run, and technical detail redaction. The core expectations are:

```python
def _failure(**overrides):
    payload = {
        "code": "model.provider_failed",
        "category": "ProviderUnavailable",
        "message_for_user": "The model provider could not complete this run.",
        "technical_detail_ref": "traceback-and-secret-must-not-render",
        "retryable": True,
    }
    payload.update(overrides)
    return payload


def test_failure_view_extracts_only_safe_bounded_fields() -> None:
    failure = vm.failure_view(_failure())
    assert failure.code == "model.provider_failed"
    assert failure.message == "The model provider could not complete this run."
    assert failure.retryable is True
    assert failure.tone == "error"
    assert "traceback" not in repr(failure)


def test_failure_view_falls_back_for_malformed_payload() -> None:
    failure = vm.failure_view({"code": 42, "message_for_user": [], "retryable": "yes"})
    assert failure.code == "runtime.failed"
    assert failure.message == "The run failed before producing a response."
    assert failure.retryable is False


def test_failed_run_view_contains_failure_but_completed_run_ignores_it() -> None:
    failed = vm.run_view(_full_snapshot(status="Failed", failure_json=_failure()))
    completed = vm.run_view(_full_snapshot(status="Completed", failure_json=_failure()))
    assert failed.failure is not None
    assert completed.failure is None
```

Run:

```powershell
uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_view_models.py -x
```

Expected RED: `failure_view` and `RunView.failure` do not exist.

- [ ] **Step 2: Implement the bounded `FailureView`**

Add beside `ApplyOutcomeView`:

```python
@dataclass(frozen=True, slots=True)
class FailureView:
    code: str
    message: str
    retryable: bool
    tone: str = "error"


def failure_view(payload: object) -> FailureView:
    fallback_code = "runtime.failed"
    fallback_message = "The run failed before producing a response."
    if type(payload) is not dict:
        return FailureView(fallback_code, fallback_message, False)
    code = payload.get("code")
    message = payload.get("message_for_user")
    retryable = payload.get("retryable")
    return FailureView(
        _bounded(code, MAX_TITLE_CHARS) or fallback_code,
        _bounded(message, MAX_BODY_CHARS) or fallback_message,
        retryable if type(retryable) is bool else False,
    )
```

Add `failure: FailureView | None` to `RunView`, and in `run_view()` set it only when `status_text == "Failed"`:

```python
failure=(
    failure_view(snapshot.get("failure_json"))
    if status_text == "Failed"
    else None
),
```

No category, `technical_detail_ref`, raw exception, or unknown mapping field may enter the view model.

- [ ] **Step 3: Write RED terminal selection tests**

Add table-driven tests for these exact results:

```python
def test_terminal_result_items_cover_every_terminal_state() -> None:
    assert [item.kind for item in vm.terminal_result_items(
        {"status": "Completed"}, "done"
    )] == ["assistant"]
    assert [item.kind for item in vm.terminal_result_items(
        {"status": "Cancelled"}, ""
    )] == ["notice"]
    assert [item.kind for item in vm.terminal_result_items(
        {"status": "Failed", "failure_json": _failure()}, ""
    )] == ["error"]
    assert [item.kind for item in vm.terminal_result_items(
        {"status": "Failed", "failure_json": _failure()}, "partial"
    )] == ["assistant", "error"]


def test_terminal_failure_card_never_leaks_technical_detail() -> None:
    items = vm.terminal_result_items(
        {"status": "Failed", "failure_json": _failure()}, ""
    )
    assert "traceback-and-secret" not in items[0].body
```

Also assert: non-terminal returns `()`; empty Completed gives an explicit normal notice; Cancelled with partial output preserves that output and does not invent an error.

- [ ] **Step 4: Implement one pure selector shared by history and live paths**

Add:

```python
def failure_card(failure: FailureView) -> MessageItem:
    retry = "\nRetry may succeed." if failure.retryable else ""
    return MessageItem(
        kind="error",
        title=_bounded(f"Run failed · {failure.code}", MAX_TITLE_CHARS),
        body=_bounded(failure.message + retry, MAX_BODY_CHARS),
        tone="error",
    )


def terminal_result_items(run: object, output: object) -> tuple[MessageItem, ...]:
    if type(run) is not dict:
        return ()
    status = run.get("status")
    text = output if type(output) is str else ""
    if status == "Completed":
        return (
            (assistant_message(text),)
            if text
            else (notice_card("The run completed without a response.", tone="normal"),)
        )
    if status == "Cancelled":
        return (
            (assistant_message(text),)
            if text
            else (notice_card("The run was cancelled.", tone="warn"),)
        )
    if status == "Failed":
        error = failure_card(failure_view(run.get("failure_json")))
        return ((assistant_message(text), error) if text else (error,))
    return ()
```

If implementation review finds that cancelled partial output also needs an explicit cancelled marker for truthfulness, adjust the RED expectation and both call sites together; do not let history/live diverge.

- [ ] **Step 5: Run focused tests and commit**

```powershell
uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_view_models.py
uv run --frozen ruff check houdini_side/runtime_panel/view_models.py tests/panel/test_runtime_panel_view_models.py
git add houdini_side/runtime_panel/view_models.py tests/panel/test_runtime_panel_view_models.py
git diff --cached --check
git commit -m "feat: normalize run failures for panel rendering"
```

---

### Task 3: Make history replay and live settling use the same selector

**Files:**

- Modify: `houdini_side/runtime_panel/main_window.py:306-396`
- Modify: `tests/panel/test_main_window_history_behavior.py`
- Modify: `tests/panel/test_main_window_history.py` if its source assertions encode the old helper
- Modify: `tests/panel/test_runtime_panel_sources.py` only if required to assert the new shared boundary

- [ ] **Step 1: Extend the AST behavior harness and write RED replay cases**

Change `_FakeViewModels` to expose `terminal_result_items(run, output)` and delegate to the real Qt-free module or return recorded fake cards matching it. Replace the old failed-empty expectation with:

```python
def test_replay_failed_without_output_emits_failure_not_empty_assistant() -> None:
    snapshot = {"runs": [{
        "run_id": "r1",
        "user_input": "broken prompt",
        "status": "Failed",
        "final_response": None,
        "failure_json": {
            "code": "model.failed",
            "message_for_user": "Provider stopped.",
            "retryable": True,
        },
    }]}
    calls, appended, seeded, shown = _run_replay(snapshot)
    assert [(item.kind, item.body) for item in appended] == [
        ("user", "broken prompt"),
        ("error", "Provider stopped.\nRetry may succeed."),
    ]
```

Add replay tests for Failed-after-partial, Cancelled-without-output, Completed, malformed failure fallback, and active terminal Run setting `_shown_output_run_id` once.

- [ ] **Step 2: Add a Qt-free live-settling behavior harness**

Extract `_maybe_render_output` with the existing AST pattern. The fake conversation must record `update_streaming`, `replace_last_assistant`, and `append_item`. Assert:

- Failed/no output replaces the trailing streaming slot with the error card;
- Failed/partial output replaces it with assistant text then appends the error card;
- repeated snapshots with the same `run_id` do nothing;
- Cancelled/no output emits the cancellation notice;
- non-terminal output only calls `update_streaming`.

Run both behavior modules and require RED against the current direct `assistant_message` calls.

- [ ] **Step 3: Replace replay branching with `terminal_result_items`**

Inside `_replay_history`, retain user-message emission and the active non-terminal streaming seed. For all terminal Runs:

```python
for item in view_models.terminal_result_items(run, final_text):
    self.conversation.append_item(item)
```

Delete the old `if final_text or is_terminal: assistant_message(...)` branch.

- [ ] **Step 4: Replace live settling with the same selector**

After the existing idempotency guard:

```python
items = view_models.terminal_result_items(shown, output)
if items:
    self.conversation.replace_last_assistant(items[0])
    for item in items[1:]:
        self.conversation.append_item(item)
self._shown_output_run_id = run_id
```

Mark the run shown even if a malformed terminal state returns no items; this prevents snapshot storms. Do not add a retry command or modify Runtime state.

- [ ] **Step 5: Run panel state, view-model, and behavior suites**

```powershell
uv run --frozen --extra eval pytest -q `
  tests/panel/test_runtime_panel_view_models.py `
  tests/panel/test_main_window_history.py `
  tests/panel/test_main_window_history_behavior.py `
  tests/panel/test_runtime_state.py `
  tests/panel/test_runtime_panel_sources.py
uv run --frozen ruff check houdini_side/runtime_panel/main_window.py tests/panel
```

Expected: all pass; no historical or live failed Run produces an empty assistant card.

- [ ] **Step 6: Review and commit the shared render path**

```powershell
git add houdini_side/runtime_panel/main_window.py tests/panel
git diff --cached --check
git commit -m "fix: show terminal run failures in conversation history"
```

---

### Task 4: Surface failure evidence in the structured Run Inspector

**Files:**

- Modify: `houdini_side/runtime_panel/inspector.py`
- Modify: `tests/panel/test_runtime_panel_sources.py`
- Modify: `tests/panel/test_runtime_panel_view_models.py` only for missing edge cases

- [ ] **Step 1: Add a RED source-boundary test**

Because PySide6 is intentionally absent from the test environment, assert the thin Qt layer consumes the typed field and does not stringify the mapping:

```python
def test_run_inspector_renders_typed_failure_block() -> None:
    source = INSPECTOR_PATH.read_text(encoding="utf-8")
    assert "if run_view.failure is not None:" in source
    assert "self._build_failure_block(run_view.failure)" in source
    assert "def _build_failure_block" in source
    assert "failure_json" not in source
```

Run the source test and expect RED.

- [ ] **Step 2: Add the failure section in semantic error styling**

In `_RunViewWidget.update_view()`, place failure immediately after timing and before plan/environment:

```python
if run_view.failure is not None:
    self._build_failure_block(run_view.failure)
```

Implement `_build_failure_block(self, failure: vm.FailureView)` using `QFrame#Card`, a 3px `STATUS_ERROR` left border, `_tone_label(..., "error")`, a word-wrapped selectable message, and a dim `"Retryable"` or `"Not retryable"` label. Render only `failure.code`, `failure.message`, and retryability. Do not accept the raw snapshot or raw mapping in this method.

- [ ] **Step 3: Run focused static/panel verification**

```powershell
uv run --frozen --extra eval pytest -q tests/panel
uv run --frozen ruff check houdini_side/runtime_panel tests/panel
uv run --frozen mypy eee_agent/panel
python -m compileall -q eee_agent houdini_side tests
```

Expected: all pass. Source inspection confirms the Run tab remains structured and separate from Apply receipt errors.

- [ ] **Step 4: Review and commit**

```powershell
git add houdini_side/runtime_panel/inspector.py tests/panel
git diff --cached --check
git commit -m "feat: show bounded failure evidence in run inspector"
```

---

### Task 5: Stage A regression, GLM diff review, and acceptance evidence

**Files:**

- Modify: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`
- Create: `docs/superpowers/reviews/2026-07-22-stage-a-acceptance.md`

- [ ] **Step 1: Audit the complete diff for scope and hidden defects**

Codex runs:

```powershell
git diff main...HEAD --stat
git diff main...HEAD -- `
  pyproject.toml eee_agent/panel houdini_side/runtime_panel tests/panel tests/runtime/test_runs.py
rg -n "technical_detail_ref|traceback|repr\(|str\(.*failure|failure_json" houdini_side/runtime_panel
```

Review requirements:

- no raw technical evidence reaches UI;
- history and live rendering call the same selector;
- no protocol/database production workaround was added;
- terminal rendering is idempotent;
- no unrelated UI redesign entered A;
- any new legacy bug or architecture defect is registered with severity/evidence/disposition.

- [ ] **Step 2: Run all fresh acceptance gates from the accepted commit**

```powershell
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision
python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
git diff --check main...HEAD
git status --short --branch
```

Expected:

- lock check, Ruff, Mypy, compileall, and diff check exit 0;
- full pytest passes with zero unexpected warnings;
- worktree is clean after committing evidence;
- skipped tests are enumerated rather than silently called passed.

- [ ] **Step 3: Write bounded acceptance evidence**

The acceptance file records exact commit, Python/uv versions, command, exit code, test counts, warning count, duration, skipped-test names/reasons, and closure evidence for RN-001 through RN-004. It must not contain `.env`, provider outputs, prompts, absolute paths with sensitive user data, or raw tracebacks.

- [ ] **Step 4: Commit evidence and make the final Stage A decision**

```powershell
git add docs/superpowers/reviews/2026-07-22-runtime-next-findings.md `
  docs/superpowers/reviews/2026-07-22-stage-a-acceptance.md
git diff --cached --check
git commit -m "docs: record stage A stability acceptance"
git status --short --branch
```

Stage A is accepted only if the final commit itself has the same passing gates or the evidence-only delta is separately checked with Ruff/diff/compile rules as applicable. Do not merge or push; hand the branch back to Codex for the B decision.
