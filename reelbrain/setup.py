"""Explicit, non-installing setup planner and toolbox bootstrap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import shutil
from typing import Callable

from .runtime_guard import RuntimeGuard
from .toolbox import ToolboxManager


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    installed: bool
    path: str | None
    required: bool
    proposed_command: str | None


@dataclass(frozen=True)
class SetupPlan:
    platform: str
    architecture: str
    certified: bool
    reelbrain_home: str
    dependencies: tuple[DependencyCheck, ...]
    network_destinations: tuple[str, ...]
    permission_summary: tuple[str, ...]
    executes_install_commands: bool = False


class SetupManager:
    def __init__(
        self,
        *,
        home: Path | str | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        configured = Path(
            home or os.environ.get("REELBRAIN_HOME", "~/.ReelBrain")
        ).expanduser()
        self.home = configured.resolve(strict=False)
        self.which = which

    def plan(self) -> SetupPlan:
        system = platform.system()
        architecture = platform.machine()
        checks = (
            self._dependency(
                "ffmpeg", required=True, command="brew install ffmpeg"
            ),
            self._dependency(
                "ffprobe", required=True, command="brew install ffmpeg"
            ),
            self._dependency(
                "whisper",
                required=False,
                command="uv tool install openai-whisper",
            ),
        )
        return SetupPlan(
            platform=system,
            architecture=architecture,
            certified=system == "Darwin" and architecture == "arm64",
            reelbrain_home=str(self.home),
            dependencies=checks,
            network_destinations=(),
            permission_summary=(
                "write immutable tools and registry below ~/.ReelBrain/toolbox",
                "run a synthetic local conformance check",
                "do not access creator footage, secrets, or providers during setup",
            ),
        )

    def apply(self, *, approved: bool) -> Path:
        if not approved:
            raise PermissionError("explicit_setup_approval_required")
        plan = self.plan()
        if not plan.certified:
            raise RuntimeError("platform_not_certified_for_v1")
        missing = [item.name for item in plan.dependencies if item.required and not item.installed]
        if missing:
            raise RuntimeError(f"required_dependencies_missing:{','.join(missing)}")
        workspace = self.home
        (workspace / "setup-conformance").mkdir(parents=True, exist_ok=True)
        guard = RuntimeGuard(
            workspace_root=workspace,
            project_id="setup-conformance",
            creator_id="local-operator",
            tool_names=("ffmpeg", "ffprobe"),
            toolbox=ToolboxManager(self.home / "toolbox"),
        )
        ffmpeg = guard.run_tool(["ffmpeg", "-version"])
        ffprobe = guard.run_tool(["ffprobe", "-version"])
        receipt = self.home / "setup-receipt.json"
        guard.authorize_path(receipt, operation="write", data_class="setup_receipt")
        receipt.write_text(
            json.dumps(
                {
                    "plan": asdict(plan),
                    "approved": True,
                    "toolbox": str(guard.toolbox.root),
                    "conformance": {
                        "ffmpeg": ffmpeg.returncode == 0,
                        "ffprobe": ffprobe.returncode == 0,
                    },
                    "install_commands_executed": [],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return receipt

    def _dependency(self, name: str, *, required: bool, command: str) -> DependencyCheck:
        path = self.which(name)
        return DependencyCheck(
            name=name,
            installed=path is not None,
            path=path,
            required=required,
            proposed_command=None if path else command,
        )
