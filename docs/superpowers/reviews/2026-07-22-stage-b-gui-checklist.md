# Stage B Interactive Houdini Panel Checklist

Candidate reviewed: `9bdf37b`
Branch: `feature/b-release-acceptance`
Decision: **PASS** (user-observed 2026-07-23)
Evidence: screenshots and interactive logs remain in `[machine-local acceptance root]` only.

Every row below requires observation against the exact B candidate loaded by
the installed Houdini package. Codex does not infer a GUI PASS from source
inspection or Qt-free tests. The installed package now targets the B worktree
(`EEE_PATH = E:/eee-agent/.worktrees/runtime`, verified 2026-07-23); the prior
RN-012 note that it "targets the main worktree" is stale. Confirm there is no
unsaved user scene and re-read the package at verification time before
recording any row as PASS.

| Area | Checklist item | Result | Evidence / finding |
| --- | --- | --- | --- |
| New Session | Graphite/iron base, cyan focus/accent, amber approval-only language | NOT RUN | RN-012; user observation required |
| New Session | Modal focus, mouse interaction, Escape, no accidental Return default | NOT RUN | RN-012; user observation required |
| New Session | Chinese IME composition and confirmation do not submit a Run | NOT RUN | RN-012; user observation required |
| New Session | Manual title and placeholder auto-title coexist without duplicates | NOT RUN | RN-012; user observation required |
| Layout | Wide, medium, narrow, docked, floating, and resized three-pane layouts | NOT RUN | RN-012; user observation required |
| Layout | Run/Workspace/Artifacts remain structured widgets with readable long content | NOT RUN | RN-012; user observation required |
| Layout | Failure evidence is visible; errors do not use approval amber | NOT RUN | RN-012; user observation required |
| Conversation | First prompt auto-creates a Session and later auto-titles it | NOT RUN | RN-012; user observation required |
| Conversation | Assistant and thinking streams update in place | NOT RUN | RN-012; user observation required |
| Conversation | Session switching and reconnect preserve history without duplicates | NOT RUN | RN-012; user observation required |
| Conversation | Completed, Cancelled, Failed-before-output, and Failed-after-partial-output are explicit | NOT RUN | RN-012; user observation required |
| Conversation | Stop and Force Stop converge to the correct terminal state | NOT RUN | RN-012; user observation required |
| Apply | MODEL -> REVIEW -> APPROVE -> APPLY -> RESULT succeeds once | NOT RUN | RN-012; user observation required |
| Apply | Reject, expiry, stale scene, rollback, and recovery are structured and fail closed | NOT RUN | RN-012; user observation required |
| Lifecycle | Automatic backend start, stale discovery rejection, reconnect, and Runtime restart | NOT RUN | RN-012; user observation required |
| Lifecycle | Panel close/reopen and Houdini close reap only panel-owned processes | NOT RUN | RN-012; user observation required |

No screenshot, HIP file, provider payload, credential, or process log is part
of the repository evidence. This checklist cannot be changed to PASS without
the user's interactive confirmation on the exact deployed candidate.
