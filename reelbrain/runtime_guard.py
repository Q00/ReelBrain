"""External reference monitor for local runtime filesystem and tool effects."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
from typing import Iterable, Mapping

from .governance import (
    ACPRegistrySnapshot,
    ACPToolIdentity,
    CapabilityBroker,
    LocalDataAccessRequest,
    ToolCapabilityGrant,
    ToolExecutionRequest,
    ToolExecutionSession,
)
from .toolbox import ManifestSigner, ToolManifest, ToolboxManager, sha256_file


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
        toolbox: ToolboxManager | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.project_id = project_id
        self.creator_id = creator_id
        self.agent_id = agent_id
        self.requester_id = "reelbrain-runtime"
        self.session_id = f"runtime:{project_id}"
        self.toolbox = toolbox or ToolboxManager()
        tools: list[ACPToolIdentity] = []
        grants: list[ToolCapabilityGrant] = []
        self._tool_by_name: dict[str, ACPToolIdentity] = {}
        self._executable_by_name: dict[str, Path] = {}
        self._official_signer = ManifestSigner(
            key_id="reelbrain-official-bootstrap-v1",
            key=b"reelbrain-official-bootstrap-verification-key-v1",
        )
        for name in tool_names:
            executable = shutil.which(name)
            if executable is None:
                continue
            system_path = Path(executable).resolve()
            try:
                toolbox_record = self.toolbox.resolve_active(name)
            except KeyError:
                bootstrap = self.toolbox.root / ".bootstrap"
                bootstrap.mkdir(parents=True, exist_ok=True)
                wrapper = bootstrap / name
                wrapper.write_text(
                    f"#!/bin/sh\nexec {shlex.quote(str(system_path))} \"$@\"\n",
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)
                unsigned = ToolManifest(
                    tool_id=name,
                    version="system-v1",
                    digest=sha256_file(wrapper),
                    origin="official",
                    entrypoint=str(system_path),
                    capabilities=("local:execute",),
                    state="approved",
                )
                manifest = self._official_signer.sign(unsigned)
                toolbox_record = self.toolbox.install_official(
                    wrapper,
                    manifest,
                    signer=self._official_signer,
                    conformance=lambda path, _: path.is_file() and os.access(path, os.X_OK),
                )
            path = toolbox_record.artifact_path
            tool = ACPToolIdentity(
                tool_id=name,
                digest=toolbox_record.manifest.digest,
                toolbox_path=path,
                lifecycle="approved",
                origin="official",
            )
            tools.append(tool)
            self._tool_by_name[name] = tool
            self._executable_by_name[name] = path
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
            governed_command = [str(self._executable_by_name[name]), *command[1:]]
            return subprocess.run(
                governed_command, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"governed_tool_failed:{exc.stderr.strip()}") from exc

    def run_callback_tool(
        self,
        *,
        tool_id: str,
        capability: str,
        dispatch,
        official: bool,
        provider: str | None = None,
        consent_receipt: Mapping[str, object] | None = None,
    ):
        """Authorize in-process agent/provider adapters through ACP before dispatch."""

        try:
            record = self.toolbox.resolve_active(tool_id)
        except KeyError:
            if not official:
                denial = {
                    "allowed": False,
                    "reason": "callback_tool_not_approved",
                    "tool_id": tool_id,
                    "capability": capability,
                }
                self.denial_logs.append(denial)
                raise PermissionError("callback_tool_not_approved")
            bootstrap = self.toolbox.root / ".bootstrap"
            bootstrap.mkdir(parents=True, exist_ok=True)
            descriptor = bootstrap / f"{tool_id}.json"
            descriptor.write_text(
                json.dumps(
                    {"tool_id": tool_id, "kind": "in_process_adapter", "capability": capability},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            manifest = self._official_signer.sign(
                ToolManifest(
                    tool_id=tool_id,
                    version="builtin-v1",
                    digest=sha256_file(descriptor),
                    origin="official",
                    entrypoint=f"python:{tool_id}",
                    capabilities=(capability,),
                    state="approved",
                )
            )
            record = self.toolbox.install_official(
                descriptor,
                manifest,
                signer=self._official_signer,
                conformance=lambda path, _: path.is_file(),
            )
        if capability not in record.manifest.capabilities:
            raise PermissionError("callback_capability_not_approved")
        if provider is not None:
            receipt = dict(consent_receipt or {})
            required = {
                "provider": provider,
                "tool_id": tool_id,
                "project_id": self.project_id,
                "creator_id": self.creator_id,
            }
            if any(receipt.get(key) != value for key, value in required.items()):
                denial = {
                    "allowed": False,
                    "reason": "provider_consent_required",
                    **required,
                }
                self.denial_logs.append(denial)
                raise PermissionError("provider_consent_required")
            if not receipt.get("data_categories") or not receipt.get("purpose"):
                raise PermissionError("provider_consent_disclosure_incomplete")
            self.provider_receipts.append(receipt)
        decision = {
            "allowed": True,
            "reason": "authorized_callback_tool",
            "tool_id": tool_id,
            "tool_digest": record.manifest.digest,
            "toolbox_path": str(record.artifact_path),
            "capabilities": [capability],
            "provider": provider,
        }
        self.capability_receipts.append(decision)
        return dispatch()

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
