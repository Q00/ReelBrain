"""External reference monitor for local runtime filesystem and tool effects."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
from typing import Iterable, Mapping

from .governance import (
    ACPRegistrySnapshot,
    ACPToolIdentity,
    ApprovedSecretStore,
    BudgetReservationReceipt,
    CapabilityBroker,
    DestinationAllowlistEntry,
    LocalDataAccessRequest,
    OutboundDestinationRequest,
    ProviderConsentReceipt,
    SecretAccessGrant,
    SecretAccessRequest,
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
                unsigned = ToolManifest(
                    tool_id=name,
                    version="system-v1",
                    digest=sha256_file(system_path),
                    origin="official",
                    entrypoint=str(system_path),
                    capabilities=("local:execute",),
                    state="approved",
                )
                manifest = self._official_signer.sign(unsigned)
                toolbox_record = self.toolbox.install_official(
                    system_path,
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
        for argument in command[1:]:
            candidate = Path(argument).expanduser()
            if candidate.is_absolute() or candidate.exists():
                self.authorize_path(
                    candidate,
                    operation="read",
                    data_class="tool_argument_path",
                )
        immutable_executable = Path(tool.toolbox_path)
        if sha256_file(immutable_executable) != tool.digest:
            raise PermissionError("registered_toolbox_artifact_digest_changed")
        try:
            governed_command = [str(immutable_executable), *command[1:]]
            sandboxed_command = self._sandbox_command(governed_command)
            sandbox_tmp = self.workspace_root / ".sandbox-tmp"
            self.authorize_path(
                sandbox_tmp, operation="write", data_class="sandbox_temporary"
            )
            sandbox_tmp.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment.update({"HOME": str(self.workspace_root), "TMPDIR": str(sandbox_tmp)})
            return subprocess.run(
                sandboxed_command,
                check=True,
                capture_output=True,
                text=True,
                preexec_fn=self._apply_resource_limits,
                env=environment,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"governed_tool_failed:{exc.stderr.strip()}") from exc

    def _sandbox_command(self, governed_command: list[str]) -> list[str]:
        if os.environ.get("REELBRAIN_DISABLE_OS_SANDBOX") == "1":
            raise PermissionError("os_sandbox_disable_denied")
        if platform.system() != "Darwin":
            raise RuntimeError("certified_os_sandbox_unavailable")
        sandbox_exec = Path("/usr/bin/sandbox-exec")
        if not sandbox_exec.is_file():
            raise RuntimeError("macos_sandbox_exec_unavailable")
        write_roots = {self.workspace_root}
        read_roots = {
            *self.broker.approved_roots,
            self.toolbox.root,
            Path("/System"),
            Path("/Library"),
            Path("/opt/homebrew"),
            Path("/usr/lib"),
            Path("/usr/share"),
            Path("/private/etc"),
            Path("/private/var/db/timezone"),
            Path.home() / "Library",
            Path.home() / ".CFUserTextEncoding",
        }
        readable_roots = {
            path.resolve(strict=False) for path in read_roots if path.exists()
        }
        readable_ancestors = {
            ancestor
            for path in readable_roots
            for ancestor in path.parents
        }
        readable_literals = {
            *readable_ancestors,
            Path("/dev/null"),
            Path("/dev/urandom"),
            Path("/dev/random"),
        }
        read_denials = " ".join(
            f'(require-not (subpath {json.dumps(str(path.resolve(strict=False)))}))'
            for path in sorted(readable_roots, key=str)
        )
        read_denials += " " + " ".join(
            f'(require-not (literal {json.dumps(str(path))}))'
            for path in sorted(readable_literals, key=str)
        )
        write_denials = " ".join(
            f'(require-not (subpath {json.dumps(str(path.resolve(strict=False)))}))'
            for path in sorted(write_roots, key=str)
        )
        profile = " ".join(
            (
                "(version 1)",
                "(allow default)",
                f"(deny file-read-data (require-all {read_denials}))",
                f'(deny file-write* (require-all {write_denials} '
                '(require-not (literal "/dev/null"))))',
                "(deny network*)",
            )
        )
        return [str(sandbox_exec), "-p", profile, *governed_command]

    @staticmethod
    def _apply_resource_limits() -> None:
        limits = (
            (resource.RLIMIT_CPU, 3600),
            (resource.RLIMIT_DATA, 4 * 1024**3),
            (resource.RLIMIT_FSIZE, 20 * 1024**3),
            (resource.RLIMIT_NOFILE, 256),
        )
        for resource_kind, soft_limit in limits:
            try:
                _, hard = resource.getrlimit(resource_kind)
                effective = soft_limit if hard == resource.RLIM_INFINITY else min(soft_limit, hard)
                resource.setrlimit(resource_kind, (effective, effective))
            except (OSError, ValueError):
                continue

    def run_callback_tool(
        self,
        *,
        tool_id: str,
        capability: str,
        dispatch,
        official: bool,
        provider: str | None = None,
        consent_receipt: Mapping[str, object] | None = None,
        destination_host: str | None = None,
        budget_reservation_receipt: Mapping[str, object] | None = None,
        secret_ref: str | None = None,
        secret_store_id: str | None = None,
        secret_store_kind: str | None = None,
        secret_store_source: str | None = None,
        secret_resolver=None,
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
        provider_receipt: dict[str, object] | None = None
        budget_receipt: dict[str, object] | None = None
        invocation_id: str | None = None
        if provider is not None:
            provider_receipt = dict(consent_receipt or {})
            required = {
                "provider": provider,
                "tool_id": tool_id,
                "project_id": self.project_id,
                "creator_id": self.creator_id,
                "destination": destination_host,
            }
            if not destination_host:
                raise PermissionError("provider_destination_required")
            if any(provider_receipt.get(key) != value for key, value in required.items()):
                denial = {
                    "allowed": False,
                    "reason": "provider_consent_required",
                    **required,
                }
                self.denial_logs.append(denial)
                raise PermissionError("provider_consent_required")
            if (
                not provider_receipt.get("data_categories")
                or not provider_receipt.get("purpose")
                or not provider_receipt.get("expected_retention")
                or not provider_receipt.get("expected_cost")
                or not provider_receipt.get("approval_receipt_id")
            ):
                raise PermissionError("provider_consent_disclosure_incomplete")
            invocation_id = str(provider_receipt.get("invocation_id") or "").strip()
            if not invocation_id:
                raise PermissionError("provider_consent_invocation_required")
            budget_receipt = dict(budget_reservation_receipt or {})
            budget_required = {
                "requester_id": self.requester_id,
                "session_id": self.session_id,
                "tool_id": tool_id,
                "project_id": self.project_id,
                "creator_id": self.creator_id,
            }
            if any(budget_receipt.get(key) != value for key, value in budget_required.items()):
                self.denial_logs.append(
                    {
                        "allowed": False,
                        "reason": "budget_reservation_required",
                        "tool_id": tool_id,
                        "provider": provider,
                    }
                )
                raise PermissionError("budget_reservation_required")

        tool_identity = ACPToolIdentity(
            tool_id=tool_id,
            digest=record.manifest.digest,
            toolbox_path=record.artifact_path,
            lifecycle="approved",
            origin=record.manifest.origin,
            human_approval_receipt_id=record.manifest.approval_receipt_id,
        )
        broker = CapabilityBroker(
            workspace_root=self.workspace_root,
            local_allowlist=self.broker.local_allowlist,
            destination_allowlist=(
                (DestinationAllowlistEntry("host", destination_host),)
                if destination_host is not None
                else ()
            ),
            provider_consents=(
                (
                    ProviderConsentReceipt(
                        provider=provider or "",
                        tool_id=tool_id,
                        project_id=self.project_id,
                        creator_id=self.creator_id,
                        invocation_id=invocation_id,
                    ),
                )
                if provider is not None
                else ()
            ),
            tool_sessions=(
                ToolExecutionSession(
                    session_id=self.session_id,
                    requester_id=self.requester_id,
                    agent_id=self.agent_id,
                    project_id=self.project_id,
                    creator_id=self.creator_id,
                ),
            ),
            tool_capability_grants=(
                ToolCapabilityGrant(
                    capability=capability,
                    tool_id=tool_id,
                    requester_id=self.requester_id,
                    session_id=self.session_id,
                    project_id=self.project_id,
                    creator_id=self.creator_id,
                ),
            ),
            budget_reservations=(
                (
                    BudgetReservationReceipt(
                        reservation_id=str(budget_receipt.get("reservation_id") or ""),
                        requester_id=str(budget_receipt.get("requester_id") or ""),
                        session_id=str(budget_receipt.get("session_id") or ""),
                        tool_id=str(budget_receipt.get("tool_id") or ""),
                        project_id=str(budget_receipt.get("project_id") or ""),
                        creator_id=str(budget_receipt.get("creator_id") or ""),
                        capabilities=tuple(budget_receipt.get("capabilities") or ()),
                        reserved_amount_cents=int(
                            budget_receipt.get("reserved_amount_cents") or 0
                        ),
                        metered_units=int(budget_receipt.get("metered_units") or 0),
                        cost_authorization_receipt_id=str(
                            budget_receipt.get("cost_authorization_receipt_id") or ""
                        ),
                        state=str(budget_receipt.get("state") or "reserved"),
                        revoked=bool(budget_receipt.get("revoked", False)),
                    ),
                )
                if provider is not None
                else ()
            ),
            secret_stores=(
                (
                    ApprovedSecretStore(
                        store_id=secret_store_id or "",
                        kind=secret_store_kind or "",
                        source=secret_store_source or "",
                    ),
                )
                if secret_ref is not None
                else ()
            ),
            secret_access_grants=(
                (
                    SecretAccessGrant(
                        store_id=secret_store_id or "",
                        secret_ref=secret_ref,
                        tool_id=tool_id,
                        execution_principal=self.requester_id,
                        project_id=self.project_id,
                        creator_id=self.creator_id,
                    ),
                )
                if secret_ref is not None
                else ()
            ),
            acp_registry=ACPRegistrySnapshot((tool_identity,)),
        )
        execution_request = ToolExecutionRequest(
            operation="execute_tool",
            requester_id=self.requester_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            project_id=self.project_id,
            creator_id=self.creator_id,
            tool_id=tool_id,
            tool_digest=record.manifest.digest,
            toolbox_path=record.artifact_path,
            capabilities=(capability,),
            paid=provider is not None,
            budget_reservation_id=(
                str(budget_receipt.get("reservation_id") or "")
                if budget_receipt is not None
                else None
            ),
            cost_authorization_receipt_id=(
                str(budget_receipt.get("cost_authorization_receipt_id") or "")
                if budget_receipt is not None
                else None
            ),
            provider=provider,
            invocation_id=invocation_id,
        )
        execution_decision = broker.authorize_tool_execution(execution_request)
        self.capability_receipts.append(asdict(execution_decision))
        if not execution_decision.allowed:
            self.denial_logs.append(asdict(execution_decision))
        execution_decision.require_allowed()

        if provider is not None:
            outbound_request = OutboundDestinationRequest(
                operation="transmit",
                provider=provider,
                agent_id=self.agent_id,
                project_id=self.project_id,
                creator_id=self.creator_id,
                host=destination_host,
                tool_id=tool_id,
                tool_digest=record.manifest.digest,
                toolbox_path=record.artifact_path,
                invocation_id=invocation_id,
            )
            outbound_decision = broker.authorize_outbound_destination(outbound_request)
            self.capability_receipts.append(asdict(outbound_decision))
            if not outbound_decision.allowed:
                self.denial_logs.append(asdict(outbound_decision))
            outbound_decision.require_allowed()
            self.provider_receipts.append(provider_receipt or {})
            reservation_record = {
                **(budget_receipt or {}),
                "state": "reserved",
                "provider": provider,
                "invocation_id": invocation_id,
            }
            self.budget_ledger.append(reservation_record)
            self.approval_records.append(
                {
                    "kind": "provider_consent",
                    "approval_receipt_id": provider_receipt.get("approval_receipt_id"),
                    "provider": provider,
                    "tool_id": tool_id,
                    "invocation_id": invocation_id,
                }
            )
        resolved_secret = None
        if secret_ref is not None:
            if not secret_store_id or not secret_store_kind or not secret_store_source:
                raise PermissionError("secret_store_required")
            if secret_resolver is None:
                raise PermissionError("secret_resolver_required")
            secret_request = SecretAccessRequest(
                operation="read_secret",
                store_id=secret_store_id,
                secret_ref=secret_ref,
                tool_id=tool_id,
                execution_principal=self.requester_id,
                agent_id=self.agent_id,
                project_id=self.project_id,
                creator_id=self.creator_id,
                tool_digest=record.manifest.digest,
                toolbox_path=record.artifact_path,
                requester_id=self.requester_id,
                session_id=self.session_id,
            )
            secret_decision = broker.authorize_secret_access(secret_request)
            self.capability_receipts.append(asdict(secret_decision))
            if not secret_decision.allowed:
                self.denial_logs.append(asdict(secret_decision))
            secret_decision.require_allowed()
            resolved_secret = secret_resolver(secret_ref)
        try:
            result = dispatch(resolved_secret) if secret_ref is not None else dispatch()
        except Exception:
            if provider is not None:
                self.budget_ledger.append(
                    {
                        **(budget_receipt or {}),
                        "state": "released_after_failure",
                        "provider": provider,
                        "invocation_id": invocation_id,
                    }
                )
            raise
        if provider is not None:
            self.budget_ledger.append(
                {
                    **(budget_receipt or {}),
                    "state": "consumed",
                    "provider": provider,
                    "invocation_id": invocation_id,
                }
            )
        return result

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
