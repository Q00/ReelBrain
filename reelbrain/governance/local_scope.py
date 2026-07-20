"""Deny-by-default authorization for non-secret local filesystem access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit

PathOperation = Literal["read", "write"]
DestinationOperation = Literal["export", "transmit"]
SecretOperation = Literal["read_secret"]
ToolExecutionOperation = Literal["execute_tool"]
ToolAssignmentOperation = Literal["assign_tool"]
ToolApprovalOperation = Literal["approve_tool"]
ContentAssetOperation = Literal["ingest_asset", "process_asset", "include_in_export_package"]
LocalExportPackageOperation = Literal["generate_export_package"]
CapabilityOperation = (
    PathOperation
    | DestinationOperation
    | SecretOperation
    | ToolExecutionOperation
    | ToolAssignmentOperation
    | ToolApprovalOperation
    | ContentAssetOperation
    | LocalExportPackageOperation
)
DestinationKind = Literal["url", "host", "bucket", "endpoint"]
SecretStoreKind = Literal["macos_keychain", "encrypted_vault"]
PayloadSurface = Literal["log", "artifact", "non_secret_field", "outbound_body", "secret_channel"]
ToolLifecycle = Literal["approved", "quarantined", "disabled", "revoked"]
ToolOrigin = Literal["official", "custom", "generated"]
ContentRightsStatus = Literal["approved", "denied", "expired", "incompatible"]
BudgetReservationState = Literal["reserved", "consumed", "released", "denied"]
DispatchResult = TypeVar("DispatchResult")

SECRET_DATA_CLASSES = frozenset({"secret", "api_secret", "vault_secret", "credential"})
SECRET_DATA_CLASS_MARKERS = (
    "secret",
    "credential",
    "api_key",
    "access_token",
    "vault",
)
SECRET_FIELD_MARKERS = (
    *SECRET_DATA_CLASS_MARKERS,
    "authorization",
    "bearer",
    "password",
    "token",
)
SECRET_REDACTION = "[REDACTED_SECRET]"
NETWORK_BACKED_CAPABILITY_MARKERS = (
    "network:",
    "provider:",
    "external:",
    "http:",
    "https:",
    "api:",
)


@dataclass(frozen=True)
class ACPToolIdentity:
    """Digest-bound tool artifact identity declared by the ACP registry."""

    tool_id: str
    digest: str
    toolbox_path: Path | str
    lifecycle: ToolLifecycle = "approved"
    origin: ToolOrigin = "official"
    human_approval_receipt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_id", self.tool_id.strip())
        object.__setattr__(self, "digest", self.digest.strip())
        object.__setattr__(
            self,
            "toolbox_path",
            Path(self.toolbox_path).expanduser().resolve(strict=False),
        )
        object.__setattr__(self, "origin", self.origin.strip())
        receipt_id = (
            self.human_approval_receipt_id.strip()
            if self.human_approval_receipt_id is not None
            else None
        )
        object.__setattr__(self, "human_approval_receipt_id", receipt_id or None)


@dataclass(frozen=True)
class ACPRegistrySnapshot:
    """Immutable ACP registry snapshot used as the sole runtime toolbox authority."""

    tools: Iterable[ACPToolIdentity] = ()
    _tools_by_identity: Mapping[tuple[str, str, Path], ACPToolIdentity] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        normalized_tools = tuple(
            ACPToolIdentity(
                tool_id=tool.tool_id,
                digest=tool.digest,
                toolbox_path=tool.toolbox_path,
                lifecycle=tool.lifecycle,
                origin=tool.origin,
                human_approval_receipt_id=tool.human_approval_receipt_id,
            )
            for tool in self.tools
        )
        tools_by_identity = {
            (tool.tool_id, tool.digest, tool.toolbox_path): tool for tool in normalized_tools
        }
        if len(tools_by_identity) != len(normalized_tools):
            raise ValueError("duplicate_acp_tool_identity")

        object.__setattr__(self, "tools", normalized_tools)
        object.__setattr__(
            self,
            "_tools_by_identity",
            MappingProxyType(tools_by_identity),
        )

    def resolve_tool(
        self,
        *,
        tool_id: str,
        digest: str,
        toolbox_path: Path | str,
    ) -> ACPToolIdentity | None:
        normalized_path = Path(toolbox_path).expanduser().resolve(strict=False)
        return self._tools_by_identity.get((tool_id.strip(), digest.strip(), normalized_path))

    def contains_tool_id(self, tool_id: str) -> bool:
        normalized_tool_id = tool_id.strip()
        return any(tool.tool_id == normalized_tool_id for tool in self.tools)

    def audit_tools(self, *, lifecycle: ToolLifecycle | None = None) -> tuple[ACPToolIdentity, ...]:
        """Return registry-visible tools for audit without implying runtime approval."""

        if lifecycle is None:
            return self.tools
        return tuple(tool for tool in self.tools if tool.lifecycle == lifecycle)

    def promote_generated_tool(
        self,
        *,
        tool_id: str,
        digest: str,
        toolbox_path: Path | str,
        human_approval_receipt_id: str,
        human_approver_id: str | None = None,
        human_confirmed: bool = False,
    ) -> ACPRegistrySnapshot:
        """Promote a quarantined generated tool after explicit human confirmation."""

        normalized_receipt_id = human_approval_receipt_id.strip()
        normalized_approver_id = (
            human_approver_id.strip() if human_approver_id is not None else ""
        )
        if not normalized_approver_id.startswith("human:"):
            raise ValueError("human_approval_required")
        if not human_confirmed:
            raise ValueError("human_confirmation_required")
        if not normalized_receipt_id:
            raise ValueError("approval_receipt_required")

        tool = self.resolve_tool(
            tool_id=tool_id,
            digest=digest,
            toolbox_path=toolbox_path,
        )
        if tool is None:
            raise ValueError("acp_tool_not_registered")
        if tool.origin != "generated":
            raise ValueError("generated_tool_required")
        if tool.lifecycle != "quarantined":
            raise ValueError("generated_tool_not_quarantined")

        promoted_tool = ACPToolIdentity(
            tool_id=tool.tool_id,
            digest=tool.digest,
            toolbox_path=tool.toolbox_path,
            lifecycle="approved",
            origin="generated",
            human_approval_receipt_id=normalized_receipt_id,
        )
        return ACPRegistrySnapshot(
            promoted_tool if registry_tool == tool else registry_tool
            for registry_tool in self.tools
        )


@dataclass(frozen=True)
class LocalDataAccessRequest:
    """A broker request to read or write local creator/project data."""

    operation: PathOperation
    path: Path | str
    data_class: str
    agent_id: str
    project_id: str
    creator_id: str


@dataclass(frozen=True)
class DestinationAllowlistEntry:
    """A destination identifier approved independently of any provider."""

    kind: DestinationKind
    value: str


@dataclass(frozen=True)
class ProviderConsentReceipt:
    """Creator consent bound to one provider/tool invocation."""

    provider: str
    tool_id: str
    project_id: str
    creator_id: str
    invocation_id: str | None = None
    revoked: bool = False


@dataclass(frozen=True)
class OutboundDestinationRequest:
    """A broker request to export or transmit data to declared outbound destinations."""

    operation: DestinationOperation
    provider: str
    agent_id: str
    project_id: str
    creator_id: str
    url: str | None = None
    host: str | None = None
    bucket: str | None = None
    endpoint: str | None = None
    tool_id: str | None = None
    tool_digest: str | None = None
    toolbox_path: Path | str | None = None
    invocation_id: str | None = None


@dataclass(frozen=True)
class ApprovedSecretStore:
    """A broker-approved source for resolving opaque secret references."""

    store_id: str
    kind: SecretStoreKind
    source: str


@dataclass(frozen=True)
class SecretAccessGrant:
    """A scoped grant binding one secret reference to one local execution principal."""

    store_id: str
    secret_ref: str
    tool_id: str
    execution_principal: str
    project_id: str
    creator_id: str
    revoked: bool = False


@dataclass(frozen=True)
class SecretAccessRequest:
    """A broker request to read a secret through an approved store reference."""

    operation: SecretOperation
    store_id: str
    secret_ref: str
    tool_id: str
    execution_principal: str
    agent_id: str
    project_id: str
    creator_id: str
    tool_digest: str | None = None
    toolbox_path: Path | str | None = None
    requester_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class ToolExecutionSession:
    """Active runtime session binding a requester to one creator/project epoch."""

    session_id: str
    requester_id: str
    agent_id: str
    project_id: str
    creator_id: str
    active: bool = True


@dataclass(frozen=True)
class ToolCapabilityGrant:
    """Scoped permission for a requester/session to execute one tool capability."""

    capability: str
    tool_id: str
    requester_id: str
    session_id: str
    project_id: str
    creator_id: str
    revoked: bool = False


@dataclass(frozen=True)
class BudgetReservationReceipt:
    """Pre-dispatch budget reservation for paid or metered local executions."""

    reservation_id: str
    requester_id: str
    session_id: str
    tool_id: str
    project_id: str
    creator_id: str
    capabilities: tuple[str, ...] = ()
    reserved_amount_cents: int = 0
    metered_units: int = 0
    cost_authorization_receipt_id: str | None = None
    state: BudgetReservationState | str = "reserved"
    revoked: bool = False


@dataclass(frozen=True)
class ToolAssignmentRequest:
    """A broker request to assign tool capabilities to a requester/session."""

    operation: ToolAssignmentOperation
    requester_id: str
    session_id: str
    agent_id: str
    project_id: str
    creator_id: str
    tool_id: str
    tool_digest: str
    toolbox_path: Path | str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ToolApprovalRequest:
    """A broker request to treat a toolbox artifact as approved for runtime use."""

    operation: ToolApprovalOperation
    approver_id: str
    agent_id: str
    project_id: str
    creator_id: str
    tool_id: str
    tool_digest: str
    toolbox_path: Path | str
    human_confirmed: bool = False
    approval_receipt_id: str | None = None


@dataclass(frozen=True)
class ToolExecutionRequest:
    """A broker request to authorize a local tool invocation before dispatch."""

    operation: ToolExecutionOperation
    requester_id: str
    session_id: str
    agent_id: str
    project_id: str
    creator_id: str
    tool_id: str
    tool_digest: str
    toolbox_path: Path | str
    capabilities: tuple[str, ...]
    paid: bool = False
    metered: bool = False
    budget_reservation_id: str | None = None
    cost_authorization_receipt_id: str | None = None
    provider: str | None = None
    invocation_id: str | None = None


@dataclass(frozen=True, repr=False)
class SecretValue:
    """Resolved secret material that may only flow through secret-typed channels."""

    value: str | bytes


@dataclass(frozen=True)
class PayloadContainmentRequest:
    """A broker request to prevent secret material from entering non-secret surfaces."""

    surface: PayloadSurface
    payload: Any = field(repr=False)
    agent_id: str
    project_id: str
    creator_id: str
    known_secret_values: tuple[str | bytes, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ContentAssetRightsReceipt:
    """Rights/licensing receipt required before a content asset can be used."""

    asset_id: str
    project_id: str
    creator_id: str
    rights_status: ContentRightsStatus | str
    allowed_operations: tuple[ContentAssetOperation, ...] = ("ingest_asset", "process_asset")
    allowed_data_classes: tuple[str, ...] = ("primary_video",)
    valid_until: date | datetime | None = None
    license_id: str | None = None


@dataclass(frozen=True)
class ContentAssetAccessRequest:
    """A broker request to ingest, process, or export a local content asset."""

    operation: ContentAssetOperation
    asset_id: str
    data_class: str
    agent_id: str
    project_id: str
    creator_id: str
    requested_at: date | datetime | None = None


@dataclass(frozen=True)
class LocalExportPackageAsset:
    """Content asset proposed for inclusion in a local export package."""

    asset_id: str
    data_class: str


@dataclass(frozen=True)
class LocalExportPackageRequest:
    """A broker request to generate a local export package."""

    operation: LocalExportPackageOperation
    package_id: str
    included_assets: tuple[LocalExportPackageAsset, ...]
    agent_id: str
    project_id: str
    creator_id: str
    requested_at: date | datetime | None = None


CapabilityRequest = (
    LocalDataAccessRequest
    | OutboundDestinationRequest
    | SecretAccessRequest
    | ToolAssignmentRequest
    | ToolApprovalRequest
    | ToolExecutionRequest
    | ContentAssetAccessRequest
    | LocalExportPackageRequest
)


@dataclass(frozen=True)
class CapabilityDecision:
    """Decision receipt for an attempted local data access."""

    allowed: bool
    reason: str
    operation: CapabilityOperation
    path: str
    approved_scope: str | None = None
    provider: str | None = None
    tool_id: str | None = None
    tool_digest: str | None = None
    toolbox_path: str | None = None
    destination: str | None = None
    secret_store_id: str | None = None
    secret_ref: str | None = None
    execution_principal: str | None = None
    requester_id: str | None = None
    session_id: str | None = None
    capabilities: tuple[str, ...] = ()
    asset_id: str | None = None
    rights_status: str | None = None
    budget_reservation_id: str | None = None
    invocation_id: str | None = None

    def require_allowed(self) -> None:
        if not self.allowed:
            raise PermissionError(self.reason)


@dataclass(frozen=True)
class PayloadContainmentDecision:
    """Decision receipt for a payload after secret containment enforcement."""

    allowed: bool
    reason: str
    surface: PayloadSurface
    sanitized_payload: Any = field(repr=False)
    secret_paths: tuple[str, ...] = ()

    def require_allowed(self) -> None:
        if not self.allowed:
            raise PermissionError(self.reason)


class LocalScopePolicy:
    """Authorize local non-secret reads/writes within approved roots only."""

    def __init__(
        self,
        *,
        workspace_root: Path | str,
        local_allowlist: Iterable[Path | str] = (),
        destination_allowlist: Iterable[DestinationAllowlistEntry] = (),
        provider_consents: Iterable[ProviderConsentReceipt] = (),
        secret_stores: Iterable[ApprovedSecretStore] = (),
        secret_access_grants: Iterable[SecretAccessGrant] = (),
        tool_sessions: Iterable[ToolExecutionSession] = (),
        tool_capability_grants: Iterable[ToolCapabilityGrant] = (),
        budget_reservations: Iterable[BudgetReservationReceipt] = (),
        content_rights_receipts: Iterable[ContentAssetRightsReceipt] = (),
        acp_registry: ACPRegistrySnapshot | None = None,
    ) -> None:
        self.workspace_root = self._canonicalize_root(workspace_root)
        self.local_allowlist = tuple(
            self._canonicalize_allowlist_root(path) for path in local_allowlist
        )
        self.approved_roots = (self.workspace_root, *self.local_allowlist)
        self.destination_allowlist = frozenset(
            self._normalize_allowlist_entry(entry) for entry in destination_allowlist
        )
        self.provider_consents = tuple(provider_consents)
        self.secret_stores = frozenset(
            self._normalize_secret_store(store) for store in secret_stores
        )
        self.secret_access_grants = tuple(secret_access_grants)
        self.tool_sessions = tuple(tool_sessions)
        self.tool_capability_grants = tuple(tool_capability_grants)
        self.budget_reservations = tuple(budget_reservations)
        self.content_rights_receipts = tuple(content_rights_receipts)
        self._acp_registry = ACPRegistrySnapshot(
            acp_registry.tools if acp_registry is not None else ()
        )

    @property
    def acp_registry(self) -> ACPRegistrySnapshot:
        """Return the broker-owned, immutable registry snapshot."""

        return self._acp_registry

    def authorize(self, request: LocalDataAccessRequest) -> CapabilityDecision:
        if request.operation not in ("read", "write"):
            return self._deny(request, "unsupported_operation")

        data_class = request.data_class.strip().lower()
        if self._is_secret_data_class(data_class):
            return self._deny(request, "secret_data_requires_vault_reference")

        try:
            requested_path = self._canonicalize_request_path(request.path)
        except (OSError, RuntimeError, ValueError):
            return CapabilityDecision(
                allowed=False,
                reason="invalid_local_path",
                operation=request.operation,
                path=str(request.path),
            )
        approved_scope = self._matching_scope(requested_path)
        if approved_scope is None:
            return CapabilityDecision(
                allowed=False,
                reason="path_outside_approved_local_scopes",
                operation=request.operation,
                path=str(requested_path),
            )

        return CapabilityDecision(
            allowed=True,
            reason="authorized_non_secret_local_scope",
            operation=request.operation,
            path=str(requested_path),
            approved_scope=str(approved_scope),
        )

    def _canonicalize_request_path(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        return candidate.resolve(strict=False)

    def _canonicalize_allowlist_root(self, path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        return candidate.resolve(strict=False)

    @staticmethod
    def _canonicalize_root(path: Path | str) -> Path:
        return Path(path).expanduser().resolve(strict=False)

    @staticmethod
    def _is_secret_data_class(data_class: str) -> bool:
        return data_class in SECRET_DATA_CLASSES or any(
            marker in data_class for marker in SECRET_DATA_CLASS_MARKERS
        )

    def _matching_scope(self, requested_path: Path) -> Path | None:
        for root in self.approved_roots:
            if requested_path == root or root in requested_path.parents:
                return root
        return None

    def _deny(self, request: LocalDataAccessRequest, reason: str) -> CapabilityDecision:
        try:
            denied_path = str(self._canonicalize_request_path(request.path))
        except (OSError, RuntimeError, ValueError):
            denied_path = str(request.path)
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path=denied_path,
        )

    @staticmethod
    def _normalize_allowlist_entry(
        entry: DestinationAllowlistEntry,
    ) -> tuple[DestinationKind, str]:
        return (entry.kind, _normalize_destination_value(entry.kind, entry.value))

    @staticmethod
    def _normalize_secret_store(store: ApprovedSecretStore) -> tuple[str, SecretStoreKind, str]:
        return (store.store_id.strip(), store.kind, store.source.strip())


class CapabilityBroker(LocalScopePolicy):
    """Broker facade for local data access and outbound destination authorization."""

    def authorize(self, request: CapabilityRequest) -> CapabilityDecision:
        """Route every broker request through the operation-specific guard."""

        if isinstance(request, LocalDataAccessRequest):
            return super().authorize(request)
        if isinstance(request, OutboundDestinationRequest):
            return self.authorize_outbound_destination(request)
        if isinstance(request, SecretAccessRequest):
            return self.authorize_secret_access(request)
        if isinstance(request, ToolAssignmentRequest):
            return self.authorize_tool_assignment(request)
        if isinstance(request, ToolApprovalRequest):
            return self.authorize_tool_approval(request)
        if isinstance(request, ToolExecutionRequest):
            return self.authorize_tool_execution(request)
        if isinstance(request, ContentAssetAccessRequest):
            return self.authorize_content_asset_access(request)
        if isinstance(request, LocalExportPackageRequest):
            return self.authorize_local_export_package(request)

        raise TypeError("unsupported_capability_request")

    def authorize_local_data_access(self, request: LocalDataAccessRequest) -> CapabilityDecision:
        return super().authorize(request)

    def authorize_outbound_destination(
        self, request: OutboundDestinationRequest
    ) -> CapabilityDecision:
        if request.operation not in ("export", "transmit"):
            return self._deny_outbound(request, "unsupported_destination_operation")

        try:
            declared_destinations = tuple(_declared_destinations(request))
        except ValueError:
            return self._deny_outbound(request, "invalid_destination_declaration")

        if not declared_destinations:
            return self._deny_outbound(request, "outbound_destination_required")

        destination_decision = self._destination_allowlist_decision(
            request,
            declared_destinations=declared_destinations,
        )
        if destination_decision is not None:
            return destination_decision

        acp_decision = self._acp_tool_decision(
            operation=request.operation,
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=request.toolbox_path,
            provider=request.provider,
        )
        if acp_decision is not None:
            return acp_decision

        consent_decision = self._provider_consent_decision(request)
        if consent_decision is not None:
            return consent_decision

        return CapabilityDecision(
            allowed=True,
            reason="authorized_outbound_destination",
            operation=request.operation,
            path="",
            approved_scope="destination_allowlist",
            provider=request.provider,
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(
                Path(request.toolbox_path).expanduser().resolve(strict=False)
                if request.toolbox_path is not None
                else ""
            ),
            destination=",".join(f"{kind}:{value}" for kind, value in declared_destinations),
            invocation_id=request.invocation_id,
        )

    def _destination_allowlist_decision(
        self,
        request: OutboundDestinationRequest,
        *,
        declared_destinations: tuple[tuple[DestinationKind, str], ...],
    ) -> CapabilityDecision | None:
        denied_destination = next(
            (
                (kind, value)
                for kind, value in declared_destinations
                if (kind, value) not in self.destination_allowlist
            ),
            None,
        )
        if denied_destination is not None:
            kind, value = denied_destination
            return CapabilityDecision(
                allowed=False,
                reason="destination_not_allowlisted",
                operation=request.operation,
                path="",
                provider=request.provider,
                tool_id=request.tool_id,
                destination=f"{kind}:{value}",
            )

        return None

    def dispatch_outbound_destination(
        self,
        request: OutboundDestinationRequest,
        dispatch: Callable[[], DispatchResult],
    ) -> DispatchResult:
        """Authorize the declared destination before performing an outbound side effect."""

        decision = self.authorize_outbound_destination(request)
        decision.require_allowed()
        return dispatch()

    def authorize_secret_access(self, request: SecretAccessRequest) -> CapabilityDecision:
        if request.operation != "read_secret":
            return self._deny_secret(request, "unsupported_secret_operation")

        store_id = request.store_id.strip()
        secret_ref = request.secret_ref.strip()
        tool_id = request.tool_id.strip()
        execution_principal = request.execution_principal.strip()

        if not store_id:
            return self._deny_secret(request, "secret_store_required")
        if not secret_ref:
            return self._deny_secret(request, "secret_reference_required")
        if not tool_id:
            return self._deny_secret(request, "local_tool_required")
        if not execution_principal:
            return self._deny_secret(request, "execution_principal_required")

        acp_decision = self._acp_tool_decision(
            operation=request.operation,
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=request.toolbox_path,
        )
        if acp_decision is not None:
            return acp_decision

        matching_stores = tuple(store for store in self.secret_stores if store[0] == store_id)
        if not matching_stores:
            return self._deny_secret(request, "secret_store_not_approved")
        if len(matching_stores) != 1:
            return self._deny_secret(request, "secret_store_source_ambiguous")
        approved_store = matching_stores[0]

        active_match = False
        revoked_match = False
        for grant in self.secret_access_grants:
            if (
                grant.store_id == store_id
                and grant.secret_ref == secret_ref
                and grant.tool_id == tool_id
                and grant.execution_principal == execution_principal
                and grant.project_id == request.project_id
                and grant.creator_id == request.creator_id
            ):
                if grant.revoked:
                    revoked_match = True
                else:
                    active_match = True

        if revoked_match:
            return self._deny_secret(request, "secret_access_grant_revoked")
        if not active_match:
            return self._deny_secret(request, "secret_access_grant_required")

        execution_context_decision = self._secret_execution_context_decision(
            request,
            execution_principal=execution_principal,
        )
        if execution_context_decision is not None:
            return execution_context_decision

        _, store_kind, store_source = approved_store
        return CapabilityDecision(
            allowed=True,
            reason="authorized_secret_store_reference",
            operation=request.operation,
            path="",
            approved_scope=f"{store_kind}:{store_source}",
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(
                Path(request.toolbox_path).expanduser().resolve(strict=False)
                if request.toolbox_path is not None
                else ""
            ),
            secret_store_id=store_id,
            secret_ref=secret_ref,
            execution_principal=execution_principal,
            requester_id=request.requester_id,
            session_id=request.session_id,
        )

    def authorize_tool_assignment(self, request: ToolAssignmentRequest) -> CapabilityDecision:
        """Authorize assignment of tool capabilities before grants are issued."""

        if request.operation != "assign_tool":
            return self._deny_tool_assignment(request, "unsupported_tool_assignment_operation")

        requester_id = request.requester_id.strip()
        session_id = request.session_id.strip()
        tool_id = request.tool_id.strip()
        requested_capabilities = tuple(
            dict.fromkeys(
                capability.strip() for capability in request.capabilities if capability.strip()
            )
        )

        if not requester_id:
            return self._deny_tool_assignment(request, "requester_required")
        if not session_id:
            return self._deny_tool_assignment(request, "tool_session_required")
        if not tool_id:
            return self._deny_tool_assignment(request, "local_tool_required")
        if not requested_capabilities:
            return self._deny_tool_assignment(request, "tool_capability_required")

        acp_decision = self._acp_tool_decision(
            operation=request.operation,
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=request.toolbox_path,
            requester_id=requester_id,
            session_id=session_id,
            capabilities=requested_capabilities,
        )
        if acp_decision is not None:
            return acp_decision

        return CapabilityDecision(
            allowed=True,
            reason="authorized_tool_assignment",
            operation=request.operation,
            path="",
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(Path(request.toolbox_path).expanduser().resolve(strict=False)),
            requester_id=requester_id,
            session_id=session_id,
            capabilities=requested_capabilities,
        )

    def authorize_tool_approval(self, request: ToolApprovalRequest) -> CapabilityDecision:
        """Authorize human-confirmed promotion of a generated toolbox artifact."""

        if request.operation != "approve_tool":
            return self._deny_tool_approval(request, "unsupported_tool_approval_operation")

        approver_id = request.approver_id.strip()
        tool_id = request.tool_id.strip()
        tool_digest = request.tool_digest.strip()
        approval_receipt_id = (
            request.approval_receipt_id.strip()
            if request.approval_receipt_id is not None
            else ""
        )
        normalized_toolbox_path = str(Path(request.toolbox_path).expanduser().resolve(strict=False))
        if not approver_id:
            return self._deny_tool_approval(request, "approver_required")
        if not tool_id:
            return self._deny_tool_approval(request, "local_tool_required")
        if not tool_digest:
            return self._deny_tool_approval(request, "acp_tool_identity_required")

        tool = self.acp_registry.resolve_tool(
            tool_id=tool_id,
            digest=tool_digest,
            toolbox_path=request.toolbox_path,
        )
        if tool is None:
            reason = (
                "acp_tool_identity_mismatch"
                if self.acp_registry.contains_tool_id(tool_id)
                else "acp_tool_not_registered"
            )
            return CapabilityDecision(
                allowed=False,
                reason=reason,
                operation=request.operation,
                path="",
                tool_id=tool_id,
                tool_digest=tool_digest,
                toolbox_path=normalized_toolbox_path,
                requester_id=approver_id,
            )

        if tool.origin != "generated":
            if tool.lifecycle == "quarantined":
                return self._deny_tool_approval(request, "acp_tool_quarantined")
            return self._deny_tool_approval(request, "generated_tool_required")
        if not approver_id.startswith("human:"):
            return self._deny_tool_approval(request, "human_approval_required")
        if not request.human_confirmed:
            return self._deny_tool_approval(request, "human_confirmation_required")
        if not approval_receipt_id:
            return self._deny_tool_approval(request, "approval_receipt_required")
        if tool.lifecycle != "quarantined":
            return self._deny_tool_approval(request, "generated_tool_not_quarantined")

        return CapabilityDecision(
            allowed=True,
            reason="authorized_tool_approval",
            operation=request.operation,
            path="",
            tool_id=tool_id,
            tool_digest=tool_digest,
            toolbox_path=normalized_toolbox_path,
            requester_id=approver_id,
        )

    def promote_approved_tool(self, request: ToolApprovalRequest) -> ACPRegistrySnapshot:
        """Return a promoted ACP snapshot after broker-authorized human approval."""

        decision = self.authorize_tool_approval(request)
        decision.require_allowed()
        return self.acp_registry.promote_generated_tool(
            tool_id=request.tool_id,
            digest=request.tool_digest,
            toolbox_path=request.toolbox_path,
            human_approval_receipt_id=request.approval_receipt_id or "",
            human_approver_id=request.approver_id,
            human_confirmed=request.human_confirmed,
        )

    def authorize_tool_execution(self, request: ToolExecutionRequest) -> CapabilityDecision:
        """Authorize requester/session/tool/grants before a tool dispatcher runs."""

        if request.operation != "execute_tool":
            return self._deny_tool_execution(request, "unsupported_tool_execution_operation")

        requester_id = request.requester_id.strip()
        session_id = request.session_id.strip()
        tool_id = request.tool_id.strip()
        requested_capabilities = tuple(
            dict.fromkeys(capability.strip() for capability in request.capabilities if capability.strip())
        )

        if not requester_id:
            return self._deny_tool_execution(request, "requester_required")
        if not session_id:
            return self._deny_tool_execution(request, "tool_session_required")
        if not tool_id:
            return self._deny_tool_execution(request, "local_tool_required")
        if not requested_capabilities:
            return self._deny_tool_execution(request, "tool_capability_required")

        session_decision = self._tool_session_decision(
            request,
            requester_id=requester_id,
            session_id=session_id,
        )
        if session_decision is not None:
            return session_decision

        acp_decision = self._acp_tool_decision(
            operation=request.operation,
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=request.toolbox_path,
            requester_id=requester_id,
            session_id=session_id,
            capabilities=requested_capabilities,
        )
        if acp_decision is not None:
            return acp_decision

        grant_decision = self._tool_capability_grant_decision(
            request,
            requester_id=requester_id,
            session_id=session_id,
            tool_id=tool_id,
            requested_capabilities=requested_capabilities,
        )
        if grant_decision is not None:
            return grant_decision

        budget_decision = self._budget_reservation_decision(
            request,
            requester_id=requester_id,
            session_id=session_id,
            tool_id=tool_id,
            requested_capabilities=requested_capabilities,
        )
        if budget_decision is not None:
            return budget_decision

        provider_consent_decision = self._tool_provider_consent_decision(
            request,
            requester_id=requester_id,
            session_id=session_id,
            tool_id=tool_id,
            requested_capabilities=requested_capabilities,
        )
        if provider_consent_decision is not None:
            return provider_consent_decision

        return CapabilityDecision(
            allowed=True,
            reason="authorized_tool_execution",
            operation=request.operation,
            path="",
            approved_scope=session_id,
            provider=request.provider.strip() if request.provider is not None else None,
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(Path(request.toolbox_path).expanduser().resolve(strict=False)),
            requester_id=requester_id,
            session_id=session_id,
            capabilities=requested_capabilities,
            budget_reservation_id=(
                request.budget_reservation_id.strip()
                if request.budget_reservation_id is not None
                else None
            ),
            invocation_id=request.invocation_id,
        )

    def dispatch_tool_execution(
        self,
        request: ToolExecutionRequest,
        dispatch: Callable[[], DispatchResult],
    ) -> DispatchResult:
        decision = self.authorize_tool_execution(request)
        decision.require_allowed()
        return dispatch()

    def authorize_content_asset_access(
        self, request: ContentAssetAccessRequest
    ) -> CapabilityDecision:
        """Block ingest/processing unless rights are present, active, and compatible."""

        if request.operation not in ("ingest_asset", "process_asset", "include_in_export_package"):
            return self._deny_content_asset(request, "unsupported_content_asset_operation")

        asset_id = request.asset_id.strip()
        data_class = request.data_class.strip().lower()
        if not asset_id:
            return self._deny_content_asset(request, "content_asset_required")
        if not data_class:
            return self._deny_content_asset(request, "content_asset_data_class_required")

        matching_receipts = tuple(
            receipt
            for receipt in self.content_rights_receipts
            if receipt.asset_id == asset_id
            and receipt.project_id == request.project_id
            and receipt.creator_id == request.creator_id
        )
        if not matching_receipts:
            return self._deny_content_asset(request, "content_rights_status_missing")

        requested_at = _normalize_content_rights_date(request.requested_at) or date.today()
        normalized_statuses = tuple(
            _normalize_content_rights_status(receipt.rights_status)
            for receipt in matching_receipts
        )
        if "" in normalized_statuses:
            return self._deny_content_asset(request, "content_rights_status_missing")
        if "denied" in normalized_statuses:
            return self._deny_content_asset(
                request,
                "content_rights_status_denied",
                rights_status="denied",
            )

        saw_expired = False
        saw_incompatible = False
        for receipt, rights_status in zip(matching_receipts, normalized_statuses, strict=True):
            if rights_status == "expired":
                saw_expired = True
                continue
            if rights_status == "incompatible":
                saw_incompatible = True
                continue
            if rights_status != "approved":
                return self._deny_content_asset(
                    request,
                    "content_rights_status_incompatible",
                    rights_status=rights_status,
                )

            valid_until = _normalize_content_rights_date(receipt.valid_until)
            if valid_until is not None and valid_until < requested_at:
                saw_expired = True
                continue

            allowed_operations = tuple(
                operation for operation in receipt.allowed_operations if operation
            )
            allowed_data_classes = tuple(
                data_class.strip().lower()
                for data_class in receipt.allowed_data_classes
                if data_class.strip()
            )
            if (
                request.operation not in allowed_operations
                or data_class not in allowed_data_classes
            ):
                saw_incompatible = True
                continue

            return CapabilityDecision(
                allowed=True,
                reason="authorized_content_asset_rights",
                operation=request.operation,
                path="",
                approved_scope=receipt.license_id,
                asset_id=asset_id,
                rights_status=rights_status,
            )

        reason = (
            "content_rights_status_expired"
            if saw_expired and not saw_incompatible
            else "content_rights_status_incompatible"
        )
        return self._deny_content_asset(request, reason)

    def dispatch_content_asset_access(
        self,
        request: ContentAssetAccessRequest,
        dispatch: Callable[[], DispatchResult],
    ) -> DispatchResult:
        """Authorize content rights before an ingest or processing side effect."""

        decision = self.authorize_content_asset_access(request)
        decision.require_allowed()
        return dispatch()

    def authorize_local_export_package(
        self, request: LocalExportPackageRequest
    ) -> CapabilityDecision:
        """Block local package generation if any included asset lacks export rights."""

        if request.operation != "generate_export_package":
            return self._deny_local_export_package(
                request,
                "unsupported_local_export_package_operation",
            )

        package_id = request.package_id.strip()
        if not package_id:
            return self._deny_local_export_package(request, "local_export_package_required")
        if not request.included_assets:
            return self._deny_local_export_package(
                request,
                "local_export_package_asset_required",
            )

        for asset in request.included_assets:
            asset_id = asset.asset_id.strip()
            data_class = asset.data_class.strip()
            if not asset_id:
                return self._deny_local_export_package(
                    request,
                    "content_asset_required",
                    asset_id=asset.asset_id,
                )
            if not data_class:
                return self._deny_local_export_package(
                    request,
                    "content_asset_data_class_required",
                    asset_id=asset_id,
                )

            asset_decision = self.authorize_content_asset_access(
                ContentAssetAccessRequest(
                    operation="include_in_export_package",
                    asset_id=asset_id,
                    data_class=data_class,
                    agent_id=request.agent_id,
                    project_id=request.project_id,
                    creator_id=request.creator_id,
                    requested_at=request.requested_at,
                )
            )
            if not asset_decision.allowed:
                return CapabilityDecision(
                    allowed=False,
                    reason=asset_decision.reason,
                    operation=request.operation,
                    path=package_id,
                    asset_id=asset_decision.asset_id,
                    rights_status=asset_decision.rights_status,
                )

        return CapabilityDecision(
            allowed=True,
            reason="authorized_local_export_package_rights",
            operation=request.operation,
            path=package_id,
            approved_scope="content_rights_receipts",
        )

    def dispatch_local_export_package(
        self,
        request: LocalExportPackageRequest,
        dispatch: Callable[[], DispatchResult],
    ) -> DispatchResult:
        """Authorize every included asset before generating a local package."""

        decision = self.authorize_local_export_package(request)
        decision.require_allowed()
        return dispatch()

    def contain_payload_secrets(
        self, request: PayloadContainmentRequest
    ) -> PayloadContainmentDecision:
        """Block or redact resolved secrets before non-secret side effects complete."""

        if request.surface == "secret_channel":
            return PayloadContainmentDecision(
                allowed=True,
                reason="authorized_secret_typed_channel",
                surface=request.surface,
                sanitized_payload=request.payload,
            )

        sanitized_payload, secret_paths = _sanitize_payload(
            request.payload,
            known_secret_values=request.known_secret_values,
            redact=request.surface in ("log", "artifact"),
        )
        if not secret_paths:
            return PayloadContainmentDecision(
                allowed=True,
                reason="no_secret_material_detected",
                surface=request.surface,
                sanitized_payload=sanitized_payload,
            )

        if request.surface in ("log", "artifact"):
            return PayloadContainmentDecision(
                allowed=True,
                reason="secret_material_redacted",
                surface=request.surface,
                sanitized_payload=sanitized_payload,
                secret_paths=secret_paths,
            )

        return PayloadContainmentDecision(
            allowed=False,
            reason="secret_material_blocked_from_non_secret_payload",
            surface=request.surface,
            sanitized_payload=None,
            secret_paths=secret_paths,
        )

    def dispatch_payload(
        self,
        request: PayloadContainmentRequest,
        dispatch: Callable[[Any], DispatchResult],
    ) -> DispatchResult:
        """Contain secrets before a payload reaches its requested side-effect sink."""

        decision = self.contain_payload_secrets(request)
        decision.require_allowed()
        return dispatch(decision.sanitized_payload)

    def _provider_consent_decision(
        self, request: OutboundDestinationRequest
    ) -> CapabilityDecision | None:
        provider = request.provider.strip()
        tool_id = request.tool_id.strip() if request.tool_id is not None else ""
        invocation_id = (
            request.invocation_id.strip() if request.invocation_id is not None else ""
        )
        if not provider:
            return self._deny_outbound(request, "provider_required")
        if not invocation_id:
            return self._deny_outbound(request, "provider_consent_required")

        active_match = False
        revoked_match = False
        for consent in self.provider_consents:
            if (
                consent.provider.strip() == provider
                and consent.tool_id.strip() == tool_id
                and consent.project_id.strip() == request.project_id.strip()
                and consent.creator_id.strip() == request.creator_id.strip()
                and consent.invocation_id is not None
                and consent.invocation_id.strip() == invocation_id
            ):
                if consent.revoked:
                    revoked_match = True
                else:
                    active_match = True

        if revoked_match:
            return self._deny_outbound(request, "provider_consent_revoked")
        if active_match:
            return None
        return self._deny_outbound(request, "provider_consent_required")

    def _tool_provider_consent_decision(
        self,
        request: ToolExecutionRequest,
        *,
        requester_id: str,
        session_id: str,
        tool_id: str,
        requested_capabilities: tuple[str, ...],
    ) -> CapabilityDecision | None:
        provider = request.provider.strip() if request.provider is not None else ""
        requires_consent = bool(provider) or _has_network_backed_capability(
            requested_capabilities
        )
        if not requires_consent:
            return None
        if not provider:
            return self._deny_tool_execution(request, "provider_required")
        invocation_id = (
            request.invocation_id.strip() if request.invocation_id is not None else ""
        )
        if not invocation_id:
            return self._deny_tool_execution(request, "provider_consent_required")

        active_match = False
        revoked_match = False
        for consent in self.provider_consents:
            if (
                consent.provider.strip() == provider
                and consent.tool_id.strip() == tool_id
                and consent.project_id.strip() == request.project_id.strip()
                and consent.creator_id.strip() == request.creator_id.strip()
                and consent.invocation_id is not None
                and consent.invocation_id.strip() == invocation_id
            ):
                if consent.revoked:
                    revoked_match = True
                else:
                    active_match = True

        if revoked_match:
            return self._deny_tool_execution(request, "provider_consent_revoked")
        if active_match:
            return None
        return self._deny_tool_execution(request, "provider_consent_required")

    def _acp_tool_decision(
        self,
        *,
        operation: CapabilityOperation,
        tool_id: str | None,
        tool_digest: str | None,
        toolbox_path: Path | str | None,
        provider: str | None = None,
        requester_id: str | None = None,
        session_id: str | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> CapabilityDecision | None:
        normalized_tool_id = tool_id.strip() if tool_id is not None else ""
        normalized_digest = tool_digest.strip() if tool_digest is not None else ""
        if not normalized_tool_id or not normalized_digest or toolbox_path is None:
            return CapabilityDecision(
                allowed=False,
                reason="acp_tool_identity_required",
                operation=operation,
                path="",
                provider=provider,
                tool_id=tool_id,
                tool_digest=tool_digest,
                toolbox_path=str(toolbox_path) if toolbox_path is not None else None,
                requester_id=requester_id,
                session_id=session_id,
                capabilities=capabilities,
            )

        tool = self.acp_registry.resolve_tool(
            tool_id=normalized_tool_id,
            digest=normalized_digest,
            toolbox_path=toolbox_path,
        )
        normalized_toolbox_path = str(Path(toolbox_path).expanduser().resolve(strict=False))
        if tool is None:
            reason = (
                "acp_tool_identity_mismatch"
                if self.acp_registry.contains_tool_id(normalized_tool_id)
                else "acp_tool_not_registered"
            )
            return CapabilityDecision(
                allowed=False,
                reason=reason,
                operation=operation,
                path="",
                provider=provider,
                tool_id=normalized_tool_id,
                tool_digest=normalized_digest,
                toolbox_path=normalized_toolbox_path,
                requester_id=requester_id,
                session_id=session_id,
                capabilities=capabilities,
            )

        if tool.lifecycle == "quarantined":
            return CapabilityDecision(
                allowed=False,
                reason="acp_tool_quarantined",
                operation=operation,
                path="",
                provider=provider,
                tool_id=tool.tool_id,
                tool_digest=tool.digest,
                toolbox_path=str(tool.toolbox_path),
                requester_id=requester_id,
                session_id=session_id,
                capabilities=capabilities,
            )

        if tool.origin == "generated" and tool.human_approval_receipt_id is None:
            return CapabilityDecision(
                allowed=False,
                reason="generated_tool_human_approval_required",
                operation=operation,
                path="",
                provider=provider,
                tool_id=tool.tool_id,
                tool_digest=tool.digest,
                toolbox_path=str(tool.toolbox_path),
                requester_id=requester_id,
                session_id=session_id,
                capabilities=capabilities,
            )

        if tool.lifecycle != "approved":
            return CapabilityDecision(
                allowed=False,
                reason="acp_tool_not_approved",
                operation=operation,
                path="",
                provider=provider,
                tool_id=tool.tool_id,
                tool_digest=tool.digest,
                toolbox_path=str(tool.toolbox_path),
                requester_id=requester_id,
                session_id=session_id,
                capabilities=capabilities,
            )

        return None

    def _tool_session_decision(
        self,
        request: ToolExecutionRequest,
        *,
        requester_id: str,
        session_id: str,
    ) -> CapabilityDecision | None:
        matching_sessions = tuple(
            session
            for session in self.tool_sessions
            if session.session_id == session_id
            and session.requester_id == requester_id
            and session.agent_id == request.agent_id
            and session.project_id == request.project_id
            and session.creator_id == request.creator_id
        )
        if not matching_sessions:
            return self._deny_tool_execution(request, "tool_session_required")
        if any(not session.active for session in matching_sessions):
            return self._deny_tool_execution(request, "tool_session_inactive")
        return None

    def _secret_execution_context_decision(
        self,
        request: SecretAccessRequest,
        *,
        execution_principal: str,
    ) -> CapabilityDecision | None:
        requester_id = request.requester_id.strip() if request.requester_id is not None else ""
        session_id = request.session_id.strip() if request.session_id is not None else ""
        if not requester_id or not session_id or requester_id != execution_principal:
            return self._deny_secret(request, "secret_execution_context_required")

        matching_session = next(
            (
                session
                for session in self.tool_sessions
                if session.session_id == session_id
                and session.requester_id == requester_id
                and session.agent_id == request.agent_id
                and session.project_id == request.project_id
                and session.creator_id == request.creator_id
            ),
            None,
        )
        if matching_session is None:
            return self._deny_secret(request, "secret_execution_context_required")
        if not matching_session.active:
            return self._deny_secret(request, "secret_execution_context_inactive")
        return None

    def _tool_capability_grant_decision(
        self,
        request: ToolExecutionRequest,
        *,
        requester_id: str,
        session_id: str,
        tool_id: str,
        requested_capabilities: tuple[str, ...],
    ) -> CapabilityDecision | None:
        active_capabilities: set[str] = set()
        revoked_capabilities: set[str] = set()
        for grant in self.tool_capability_grants:
            capability = grant.capability.strip()
            if (
                capability in requested_capabilities
                and grant.tool_id == tool_id
                and grant.requester_id == requester_id
                and grant.session_id == session_id
                and grant.project_id == request.project_id
                and grant.creator_id == request.creator_id
            ):
                if grant.revoked:
                    revoked_capabilities.add(capability)
                else:
                    active_capabilities.add(capability)

        revoked_requested_capabilities = tuple(
            capability
            for capability in requested_capabilities
            if capability in revoked_capabilities
        )
        if revoked_requested_capabilities:
            return CapabilityDecision(
                allowed=False,
                reason="tool_capability_grant_revoked",
                operation=request.operation,
                path="",
                tool_id=tool_id,
                tool_digest=request.tool_digest,
                toolbox_path=str(Path(request.toolbox_path).expanduser().resolve(strict=False)),
                requester_id=requester_id,
                session_id=session_id,
                capabilities=revoked_requested_capabilities,
            )

        missing_capabilities = tuple(
            capability
            for capability in requested_capabilities
            if capability not in active_capabilities
        )
        if not missing_capabilities:
            return None

        return CapabilityDecision(
            allowed=False,
            reason="tool_capability_grant_required",
            operation=request.operation,
            path="",
            tool_id=tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(Path(request.toolbox_path).expanduser().resolve(strict=False)),
            requester_id=requester_id,
            session_id=session_id,
            capabilities=missing_capabilities,
        )

    def _budget_reservation_decision(
        self,
        request: ToolExecutionRequest,
        *,
        requester_id: str,
        session_id: str,
        tool_id: str,
        requested_capabilities: tuple[str, ...],
    ) -> CapabilityDecision | None:
        if not _requires_budget_reservation(request, requested_capabilities):
            return None

        reservation_id = (
            request.budget_reservation_id.strip()
            if request.budget_reservation_id is not None
            else ""
        )
        if not reservation_id:
            return self._deny_tool_execution(request, "budget_reservation_required")

        matching_reservation = next(
            (
                reservation
                for reservation in self.budget_reservations
                if reservation.reservation_id.strip() == reservation_id
            ),
            None,
        )
        if matching_reservation is None:
            return self._deny_tool_execution(request, "budget_reservation_not_found")

        if matching_reservation.revoked:
            return self._deny_tool_execution(request, "budget_reservation_revoked")
        if matching_reservation.state.strip().lower() != "reserved":
            return self._deny_tool_execution(request, "budget_reservation_inactive")
        if (
            matching_reservation.reserved_amount_cents < 0
            or matching_reservation.metered_units < 0
        ):
            return self._deny_tool_execution(request, "budget_reservation_amount_invalid")
        if request.paid and matching_reservation.reserved_amount_cents <= 0:
            return self._deny_tool_execution(request, "budget_reservation_amount_required")
        if (
            matching_reservation.reserved_amount_cents == 0
            and matching_reservation.metered_units == 0
        ):
            return self._deny_tool_execution(request, "budget_reservation_amount_required")

        reserved_capabilities = tuple(
            dict.fromkeys(
                capability.strip()
                for capability in matching_reservation.capabilities
                if capability.strip()
            )
        )
        scope_matches = (
            matching_reservation.requester_id == requester_id
            and matching_reservation.session_id == session_id
            and matching_reservation.tool_id == tool_id
            and matching_reservation.project_id == request.project_id
            and matching_reservation.creator_id == request.creator_id
            and (
                not reserved_capabilities
                or all(capability in reserved_capabilities for capability in requested_capabilities)
            )
        )
        if not scope_matches:
            return self._deny_tool_execution(request, "budget_reservation_scope_mismatch")

        cost_authorization_receipt_id = (
            matching_reservation.cost_authorization_receipt_id.strip()
            if matching_reservation.cost_authorization_receipt_id is not None
            else ""
        )
        request_cost_authorization_receipt_id = (
            request.cost_authorization_receipt_id.strip()
            if request.cost_authorization_receipt_id is not None
            else ""
        )
        if not cost_authorization_receipt_id:
            return self._deny_tool_execution(request, "cost_authorization_required")
        if not request_cost_authorization_receipt_id:
            return self._deny_tool_execution(request, "cost_authorization_required")
        if request_cost_authorization_receipt_id != cost_authorization_receipt_id:
            return self._deny_tool_execution(request, "cost_authorization_scope_mismatch")

        return None

    @staticmethod
    def _deny_outbound(
        request: OutboundDestinationRequest, reason: str
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            provider=request.provider,
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(request.toolbox_path) if request.toolbox_path is not None else None,
            invocation_id=request.invocation_id,
        )

    @staticmethod
    def _deny_secret(request: SecretAccessRequest, reason: str) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(request.toolbox_path) if request.toolbox_path is not None else None,
            secret_store_id=request.store_id,
            secret_ref=request.secret_ref,
            execution_principal=request.execution_principal,
            requester_id=request.requester_id,
            session_id=request.session_id,
        )

    @staticmethod
    def _deny_tool_execution(
        request: ToolExecutionRequest, reason: str
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            provider=request.provider,
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(request.toolbox_path),
            requester_id=request.requester_id,
            session_id=request.session_id,
            capabilities=tuple(request.capabilities),
            budget_reservation_id=(
                request.budget_reservation_id.strip()
                if request.budget_reservation_id is not None
                else None
            ),
            invocation_id=request.invocation_id,
        )

    @staticmethod
    def _deny_tool_assignment(
        request: ToolAssignmentRequest, reason: str
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(request.toolbox_path),
            requester_id=request.requester_id,
            session_id=request.session_id,
            capabilities=tuple(request.capabilities),
        )

    @staticmethod
    def _deny_tool_approval(
        request: ToolApprovalRequest, reason: str
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            tool_id=request.tool_id,
            tool_digest=request.tool_digest,
            toolbox_path=str(request.toolbox_path),
            requester_id=request.approver_id,
        )

    @staticmethod
    def _deny_content_asset(
        request: ContentAssetAccessRequest,
        reason: str,
        *,
        rights_status: str | None = None,
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path="",
            asset_id=request.asset_id,
            rights_status=rights_status,
        )

    @staticmethod
    def _deny_local_export_package(
        request: LocalExportPackageRequest,
        reason: str,
        *,
        asset_id: str | None = None,
    ) -> CapabilityDecision:
        return CapabilityDecision(
            allowed=False,
            reason=reason,
            operation=request.operation,
            path=request.package_id,
            asset_id=asset_id,
        )


def _declared_destinations(
    request: OutboundDestinationRequest,
) -> Iterable[tuple[DestinationKind, str]]:
    for kind in ("url", "host", "bucket", "endpoint"):
        value = getattr(request, kind)
        if value is not None and value.strip():
            yield (kind, _normalize_destination_value(kind, value))


def _normalize_destination_value(kind: DestinationKind, value: str) -> str:
    if kind == "url":
        return _normalize_url(value)
    if kind == "host":
        return _normalize_host(value)
    return value.strip()


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("url destinations must include scheme and host")

    netloc = _normalize_host(parsed.hostname)
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _normalize_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not host:
        raise ValueError("host destinations must be non-empty")
    return host


def _has_network_backed_capability(capabilities: Iterable[str]) -> bool:
    return any(
        capability.strip().lower().startswith(NETWORK_BACKED_CAPABILITY_MARKERS)
        for capability in capabilities
    )


def _requires_budget_reservation(
    request: ToolExecutionRequest,
    requested_capabilities: Iterable[str],
) -> bool:
    provider = request.provider.strip() if request.provider is not None else ""
    return (
        request.paid
        or request.metered
        or bool(provider)
        or _has_network_backed_capability(requested_capabilities)
    )


def _normalize_content_rights_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _normalize_content_rights_status(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower()


def _sanitize_payload(
    payload: Any,
    *,
    known_secret_values: Iterable[str | bytes],
    redact: bool,
) -> tuple[Any, tuple[str, ...]]:
    secrets = tuple(secret for secret in known_secret_values if secret not in ("", b""))
    sanitized, paths = _sanitize_payload_value(
        payload,
        path="$",
        known_secret_values=secrets,
        redact=redact,
    )
    return sanitized, tuple(paths)


def _sanitize_payload_value(
    value: Any,
    *,
    path: str,
    known_secret_values: tuple[str | bytes, ...],
    redact: bool,
) -> tuple[Any, list[str]]:
    if isinstance(value, SecretValue):
        return (SECRET_REDACTION if redact else value, [path])

    if isinstance(value, Mapping):
        sanitized_mapping: dict[Any, Any] = {}
        secret_paths: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if _is_secret_field_name(key):
                sanitized_mapping[key] = SECRET_REDACTION if redact else item
                secret_paths.append(child_path)
                continue

            sanitized_item, child_secret_paths = _sanitize_payload_value(
                item,
                path=child_path,
                known_secret_values=known_secret_values,
                redact=redact,
            )
            sanitized_mapping[key] = sanitized_item
            secret_paths.extend(child_secret_paths)
        return sanitized_mapping, secret_paths

    if isinstance(value, tuple):
        sanitized_items, secret_paths = _sanitize_sequence(
            value,
            path=path,
            known_secret_values=known_secret_values,
            redact=redact,
        )
        return tuple(sanitized_items), secret_paths

    if isinstance(value, list):
        return _sanitize_sequence(
            value,
            path=path,
            known_secret_values=known_secret_values,
            redact=redact,
        )

    if isinstance(value, str):
        redacted_value, matched = _redact_known_string_secrets(value, known_secret_values)
        if matched:
            return (redacted_value if redact else value, [path])
        return value, []

    if isinstance(value, bytes):
        redacted_value, matched = _redact_known_byte_secrets(value, known_secret_values)
        if matched:
            return (redacted_value if redact else value, [path])
        return value, []

    return value, []


def _sanitize_sequence(
    values: Iterable[Any],
    *,
    path: str,
    known_secret_values: tuple[str | bytes, ...],
    redact: bool,
) -> tuple[list[Any], list[str]]:
    sanitized_items: list[Any] = []
    secret_paths: list[str] = []
    for index, item in enumerate(values):
        sanitized_item, child_secret_paths = _sanitize_payload_value(
            item,
            path=f"{path}[{index}]",
            known_secret_values=known_secret_values,
            redact=redact,
        )
        sanitized_items.append(sanitized_item)
        secret_paths.extend(child_secret_paths)
    return sanitized_items, secret_paths


def _is_secret_field_name(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_FIELD_MARKERS)


def _redact_known_string_secrets(
    value: str, known_secret_values: tuple[str | bytes, ...]
) -> tuple[str, bool]:
    redacted = value
    matched = False
    for secret in known_secret_values:
        if isinstance(secret, str) and secret and secret in redacted:
            redacted = redacted.replace(secret, SECRET_REDACTION)
            matched = True
    return redacted, matched


def _redact_known_byte_secrets(
    value: bytes, known_secret_values: tuple[str | bytes, ...]
) -> tuple[bytes, bool]:
    redacted = value
    matched = False
    redaction = SECRET_REDACTION.encode()
    for secret in known_secret_values:
        if isinstance(secret, bytes) and secret and secret in redacted:
            redacted = redacted.replace(secret, redaction)
            matched = True
    return redacted, matched
