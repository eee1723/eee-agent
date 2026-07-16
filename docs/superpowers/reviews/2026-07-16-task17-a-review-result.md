# Task 17-A Acceptance Result

- Review date: 2026-07-16 (Asia/Shanghai)
- Implementation: `931ac1c`, `ffa069f`
- Houdini worker-loop correction: `6685b72`
- Result: **Accepted**

## Delivered boundary

Task 17-A delivers a dockable, read-only Houdini Runtime observer and selection
inspector. It authenticates independently to Runtime and the Secure Bridge,
resumes Runtime subscriptions from per-Session cursors, obtains an exact
SceneBinding before `scene.query`, and displays HIP, instance, epoch, revision,
node identity, lock state, and bounded geometry facts.

The panel does not own Runtime, open SQLite, access HOM directly, expose Apply
or approval actions, import legacy rpyc, or mutate the scene.

## Automated evidence

- Panel tests: **31 passed**
- Task 17-A2 plan gate: **123 passed**
- Panel/Bridge/server focused regression: **347 passed**
- Full offline suite: **2068 passed, 1 skipped**
- `uv lock --check`: passed, 69 packages
- compileall and `git diff --check`: passed
- The only skip is the existing optional WSL environment probe.

## Real Houdini evidence

The first real Houdini start identified that its process-wide
`haio.HoudiniEventLoopPolicy` returns a main-thread-only singleton loop even
from worker threads. The correction constructs isolated stdlib selector loops
for both Secure Bridge transport and selection refresh. A real Houdini 21.0.440
bundled-Python loopback and host-lifecycle probe passed under the installed
`haio` policy.

After restarting Houdini, the user confirmed:

- zero, one, and multiple node selections match the panel;
- node paths, types, geometry facts, scene epoch, and revision render;
- Runtime restart reconnects successfully;
- closing and reopening the panel preserves the Runtime/Bridge lifecycle;
- no Houdini scene or filesystem mutation is observed.

The user reported the complete checklist as “全部通过”. Task 17-A is accepted.
Task 17-B may begin only under its own bounded design and plan.
