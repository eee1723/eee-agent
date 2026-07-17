from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    home: Path
    state_dir: Path
    app_db: Path
    checkpoints_db: Path
    lock_file: Path
    discovery_file: Path
    token_file: Path
    artifacts_dir: Path

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        override = os.getenv("EEE_RUNTIME_HOME")
        if override is not None:
            if not override.strip():
                raise ValueError("EEE_RUNTIME_HOME must not be empty")
            home = Path(override)
            if not home.is_absolute():
                raise ValueError("EEE_RUNTIME_HOME must be absolute")
            # Inspect the raw path before resolve(); resolve() would normalize a
            # `..` segment away (approved spec rejects parent traversal).
            if ".." in home.parts:
                raise ValueError("EEE_RUNTIME_HOME must not contain parent traversal")
        else:
            local = os.getenv("LOCALAPPDATA")
            if not local:
                raise ValueError("LOCALAPPDATA is required when EEE_RUNTIME_HOME is unset")
            home = Path(local) / "EEEAgent"
        home = home.resolve()
        if home.exists() and not home.is_dir():
            raise ValueError("Runtime home must be a directory")
        state = home / "state"
        return cls(
            home=home,
            state_dir=state,
            app_db=state / "app.sqlite",
            checkpoints_db=state / "checkpoints.sqlite",
            lock_file=state / "runtime.lock",
            discovery_file=state / "runtime.json",
            token_file=state / "runtime.token",
            artifacts_dir=state / "artifacts",
        )

    def create_used_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
