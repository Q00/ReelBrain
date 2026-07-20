"""External reference monitor for local runtime filesystem and tool effects."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from .governance import (
    ACPRegistrySnapshot,
    ACPToolIdentity,
    CapabilityBroker,
    LocalDataAccessRequest,
    ToolCapabilityGrant,
    ToolExecutionRequest,
    ToolExecutionSession,
)


def executable_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


class RuntimeGuard:
    """Deny-by-default gateway used before every production side effect."""

    def __init__(
        self,
        *,
        workspace_root: Path | str,
        local_allowlist: Iterable[Path | str] = (),
        project_id: str,
        creator_id: str,
        agent_id: str = "showrunner",
        tool_names: Iterable[str] = ("ffmpeg", "ffprobe"),
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.project_id = project_id
        self.creator_id = creator_id
        self.agent_id = agent_id
        self.requester_id = "reelbrain-runtime"
        self.session_id = f"runtime:{project_id}"
        tools: list[ACPToolIdentity] = []
        grants: list[ToolCapabilityGrant] = []
        self._tool_by_name: dict[str, ACPToolIdentity] = {}
        for name in tool_names:
            executable = shutil.which(name)
            if executable is None:
                continue
            path = Path(executable).resolve()
            tool = ACPToolIdentity(
                tool_id=name,
                digest=executable_digest(path),
                toolbox_path=path,
                lifecycle="approved",
                origin="official",
            )
            tools.append(tool)
            self._tool_by_name[name] = tool
            grants.append(
                ToolCapabilityGrant(
                    capability="local:execute",
                    tool_id=name,
                    requester_id=self.requester_id,
                    session_id=self.session_id,
                    project_id=project_id,
                    creator_id=creator_id,
                )
            )
        self.registry = ACPRegistrySnapshot(tools)
        self.broker = CapabilityBroker(
            workspace_root=self.workspace_root,
            local_allowlist=local_allowlist,
            tool_sessions=(
                ToolExecutionSession(
                    session_id=self.session_id,
                    requester_id=self.requester_id,
                    agent_id=agent_id,
                    project_id=project_id,
                    creator_id=creator_id,
                ),
            ),
            tool_capability_grants=grants,
            acp_registry=self.registry,
        )
        self.capability_receipts: list[dict[str, object]] = []
        self.denial_logs: list[dict[str, object]] = []
        self.provider_receipts: list[dict[str, object]] = []
        self.budget_ledger: list[dict[str, object]] = []
        self.approval_records: list[dict[str, object]] = []

    def authorize_path(self, path: Path | str, *, operation: str, data_class: str) -> None:
        request = LocalDataAccessRequest(
            operation=operation,
            path=path,
            data_class=data_class,
            agent_id=self.agent_id,
            project_id=self.project_id,
            creator_id=self.creator_id,
        )
        decision = self.broker.authorize_local_data_access(request)
        payload = asdict(decision)
        self.capability_receipts.append(payload)
        if not decision.allowed:
            self.denial_logs.append(payload)
        decision.require_allowed()

    def run_tool(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        name = Path(command[0]).name
        tool = self._tool_by_name.get(name)
        if tool is None:
            raise PermissionError("tool_not_registered_in_acp")
        request = ToolExecutionRequest(
            operation="execute_tool",
            requester_id=self.requester_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            project_id=self.project_id,
            creator_id=self.creator_id,
            tool_id=tool.tool_id,
            tool_digest=tool.digest,
            toolbox_path=tool.toolbox_path,
            capabilities=("local:execute",),
        )
        decision = self.broker.authorize_tool_execution(request)
        payload = asdict(decision)
        self.capability_receipts.append(payload)
        if not decision.allowed:
            self.denial_logs.append(payload)
        decision.require_allowed()
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"governed_tool_failed:{exc.stderr.strip()}") from exc

    def write_audit_artifacts(
        self,
        output_dir: Path | str,
        *,
        rights_manifest: list[dict[str, object]] | None = None,
    ) -> dict[str, Path]:
        root = Path(output_dir).resolve()
        self.authorize_path(root, operation="write", data_class="audit_directory")
        root.mkdir(parents=True, exist_ok=True)
        documents = {
            "acp_registry": [asdict(tool) for tool in self.registry.tools],
            "capability_receipts": self.capability_receipts,
            "toolbox_manifests": [
                {
                    "tool_id": tool.tool_id,
                    "digest": tool.digest,
                    "path": str(tool.toolbox_path),
                    "lifecycle": tool.lifecycle,
                }
                for tool in self.registry.tools
            ],
            "provider_receipts": self.provider_receipts,
            "budget_ledger": self.budget_ledger,
            "rights_manifest": rights_manifest or [],
            "denial_logs": self.denial_logs,
            "approval_records": self.approval_records,
        }
        artifacts: dict[str, Path] = {}
        for name, document in documents.items():
            path = root / f"{name}.json"
            self.authorize_path(path, operation="write", data_class="audit_record")
            path.write_text(
                json.dumps(document, indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
            artifacts[name] = path
        return artifacts
