from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from reelbrain.governance import (
    ApprovedSecretStore,
    ACPRegistrySnapshot,
    ACPToolIdentity,
    BudgetReservationReceipt,
    CapabilityBroker,
    ContentAssetAccessRequest,
    ContentAssetRightsReceipt,
    DestinationAllowlistEntry,
    LocalExportPackageAsset,
    LocalExportPackageRequest,
    LocalDataAccessRequest,
    LocalScopePolicy,
    OutboundDestinationRequest,
    PayloadContainmentRequest,
    ProviderConsentReceipt,
    SecretAccessGrant,
    SecretAccessRequest,
    SecretValue,
    ToolApprovalRequest,
    ToolAssignmentRequest,
    ToolCapabilityGrant,
    ToolExecutionRequest,
    ToolExecutionSession,
)


TOOL_DIGESTS = {
    "caption-api": "sha256:caption-api",
    "caption-provider-client": "sha256:caption-provider-client",
    "cdn-relay": "sha256:cdn-relay",
    "export-relay": "sha256:export-relay",
    "generated-editor": "sha256:generated-editor",
}


def tool_path(tool_id: str) -> str:
    return f"~/.ReelBrain/toolbox/{tool_id}"


def approved_tool(
    tool_id: str,
    *,
    lifecycle: str = "approved",
    origin: str = "official",
    human_approval_receipt_id: str | None = None,
) -> ACPToolIdentity:
    return ACPToolIdentity(
        tool_id=tool_id,
        digest=TOOL_DIGESTS[tool_id],
        toolbox_path=tool_path(tool_id),
        lifecycle=lifecycle,
        origin=origin,
        human_approval_receipt_id=human_approval_receipt_id,
    )


def acp_registry(*tools: ACPToolIdentity) -> ACPRegistrySnapshot:
    return ACPRegistrySnapshot(
        tools
        or (
            approved_tool("caption-api"),
            approved_tool("caption-provider-client"),
            approved_tool("cdn-relay"),
            approved_tool("export-relay"),
        )
    )


def request(path: Path | str, *, operation: str = "read", data_class: str = "transcript"):
    return LocalDataAccessRequest(
        operation=operation,
        path=path,
        data_class=data_class,
        agent_id="meaning-scout",
        project_id="project-1",
        creator_id="creator-1",
    )


def content_asset_request(
    *,
    operation: str = "ingest_asset",
    asset_id: str = "asset-primary-video",
    data_class: str = "primary_video",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    requested_at: date = date(2026, 7, 20),
):
    return ContentAssetAccessRequest(
        operation=operation,
        asset_id=asset_id,
        data_class=data_class,
        agent_id="showrunner",
        project_id=project_id,
        creator_id=creator_id,
        requested_at=requested_at,
    )


def content_rights_receipt(
    *,
    asset_id: str = "asset-primary-video",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    rights_status: str = "approved",
    allowed_operations: tuple[str, ...] = ("ingest_asset", "process_asset"),
    allowed_data_classes: tuple[str, ...] = ("primary_video",),
    valid_until: date | None = date(2026, 12, 31),
    license_id: str | None = "license-1",
):
    return ContentAssetRightsReceipt(
        asset_id=asset_id,
        project_id=project_id,
        creator_id=creator_id,
        rights_status=rights_status,
        allowed_operations=allowed_operations,
        allowed_data_classes=allowed_data_classes,
        valid_until=valid_until,
        license_id=license_id,
    )


def local_export_package_request(
    *,
    package_id: str = "package-short-1",
    included_assets: tuple[LocalExportPackageAsset, ...] = (
        LocalExportPackageAsset("asset-primary-video", "primary_video"),
    ),
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    requested_at: date = date(2026, 7, 20),
):
    return LocalExportPackageRequest(
        operation="generate_export_package",
        package_id=package_id,
        included_assets=included_assets,
        agent_id="showrunner",
        project_id=project_id,
        creator_id=creator_id,
        requested_at=requested_at,
    )


def test_authorizes_non_secret_reads_and_writes_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = LocalScopePolicy(workspace_root=workspace)

    read_decision = policy.authorize(request("project/transcript.json", data_class="transcript"))
    write_decision = policy.authorize(
        request(workspace / "exports" / "manifest.json", operation="write", data_class="manifest")
    )

    assert read_decision.allowed is True
    assert read_decision.reason == "authorized_non_secret_local_scope"
    assert read_decision.approved_scope == str(workspace.resolve())
    assert write_decision.allowed is True


def test_authorizes_content_asset_ingest_and_processing_with_active_compatible_rights(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[content_rights_receipt()],
    )

    ingest_decision = broker.authorize_content_asset_access(content_asset_request())
    processing_decision = broker.authorize_content_asset_access(
        content_asset_request(operation="process_asset")
    )

    assert ingest_decision.allowed is True
    assert ingest_decision.reason == "authorized_content_asset_rights"
    assert ingest_decision.asset_id == "asset-primary-video"
    assert ingest_decision.rights_status == "approved"
    assert ingest_decision.approved_scope == "license-1"
    assert processing_decision.allowed is True


def test_blocks_content_asset_use_when_rights_status_is_missing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    no_receipt_broker = CapabilityBroker(workspace_root=workspace)
    blank_status_broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[content_rights_receipt(rights_status="")],
    )

    no_receipt = no_receipt_broker.authorize_content_asset_access(content_asset_request())
    blank_status = blank_status_broker.authorize_content_asset_access(content_asset_request())

    assert no_receipt.allowed is False
    assert no_receipt.reason == "content_rights_status_missing"
    assert blank_status.allowed is False
    assert blank_status.reason == "content_rights_status_missing"


def test_blocks_content_asset_use_when_rights_are_expired_incompatible_or_denied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    expired = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(valid_until=date(2026, 7, 19)),
        ],
    ).authorize_content_asset_access(content_asset_request())
    incompatible_status = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(rights_status="incompatible"),
        ],
    ).authorize_content_asset_access(content_asset_request())
    incompatible_operation = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(allowed_operations=("process_asset",)),
        ],
    ).authorize_content_asset_access(content_asset_request())
    denied = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(rights_status="denied"),
        ],
    ).authorize_content_asset_access(content_asset_request())

    assert expired.allowed is False
    assert expired.reason == "content_rights_status_expired"
    assert incompatible_status.allowed is False
    assert incompatible_status.reason == "content_rights_status_incompatible"
    assert incompatible_operation.allowed is False
    assert incompatible_operation.reason == "content_rights_status_incompatible"
    assert denied.allowed is False
    assert denied.reason == "content_rights_status_denied"


def test_generic_authorize_blocks_content_asset_ingest_and_processing_with_invalid_rights(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    missing = CapabilityBroker(workspace_root=workspace).authorize(content_asset_request())
    expired_processing = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(valid_until=date(2026, 7, 19)),
        ],
    ).authorize(content_asset_request(operation="process_asset"))
    denied_processing = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(rights_status="denied"),
        ],
    ).authorize(content_asset_request(operation="process_asset"))
    incompatible_ingest = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(allowed_operations=("include_in_export_package",)),
        ],
    ).authorize(content_asset_request())

    assert missing.allowed is False
    assert missing.reason == "content_rights_status_missing"
    assert expired_processing.allowed is False
    assert expired_processing.reason == "content_rights_status_expired"
    assert denied_processing.allowed is False
    assert denied_processing.reason == "content_rights_status_denied"
    assert incompatible_ingest.allowed is False
    assert incompatible_ingest.reason == "content_rights_status_incompatible"


@pytest.mark.parametrize(
    ("receipts", "expected_reason"),
    [
        ((), "content_rights_status_missing"),
        (
            (content_rights_receipt(valid_until=date(2026, 7, 19)),),
            "content_rights_status_expired",
        ),
        (
            (content_rights_receipt(allowed_operations=()),),
            "content_rights_status_incompatible",
        ),
        (
            (content_rights_receipt(rights_status="denied"),),
            "content_rights_status_denied",
        ),
    ],
)
@pytest.mark.parametrize("operation", ["ingest_asset", "process_asset"])
def test_content_asset_dispatch_blocks_invalid_rights_before_ingest_or_processing_side_effect(
    tmp_path,
    receipts,
    expected_reason,
    operation,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=receipts,
    )
    dispatch_calls = []

    with pytest.raises(PermissionError, match=expected_reason):
        broker.dispatch_content_asset_access(
            content_asset_request(operation=operation),
            lambda: dispatch_calls.append("processed"),
        )

    assert dispatch_calls == []


def test_content_asset_dispatch_runs_after_active_compatible_rights_check(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=(content_rights_receipt(),),
    )
    dispatch_calls = []

    result = broker.dispatch_content_asset_access(
        content_asset_request(operation="ingest_asset"),
        lambda: dispatch_calls.append("ingested") or "asset-record",
    )

    assert result == "asset-record"
    assert dispatch_calls == ["ingested"]


def test_explicit_denial_overrides_an_approved_receipt_for_the_same_asset(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=(
            content_rights_receipt(),
            content_rights_receipt(rights_status="denied"),
        ),
    )

    decision = broker.authorize_content_asset_access(content_asset_request())

    assert decision.allowed is False
    assert decision.reason == "content_rights_status_denied"
    assert decision.rights_status == "denied"


def test_authorizes_local_export_package_when_all_included_assets_have_rights(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(
                allowed_operations=(
                    "ingest_asset",
                    "process_asset",
                    "include_in_export_package",
                ),
            ),
            content_rights_receipt(
                asset_id="asset-thumbnail",
                allowed_operations=("include_in_export_package",),
                allowed_data_classes=("thumbnail",),
                license_id="license-thumbnail",
            ),
        ],
    )

    decision = broker.authorize_local_export_package(
        local_export_package_request(
            included_assets=(
                LocalExportPackageAsset("asset-primary-video", "primary_video"),
                LocalExportPackageAsset("asset-thumbnail", "thumbnail"),
            )
        )
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_local_export_package_rights"
    assert decision.path == "package-short-1"
    assert decision.approved_scope == "content_rights_receipts"


def test_blocks_local_export_package_generation_for_failing_asset_rights(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(
                allowed_operations=("include_in_export_package",),
            ),
            content_rights_receipt(
                asset_id="asset-background-music",
                rights_status="denied",
                allowed_operations=("include_in_export_package",),
                allowed_data_classes=("music",),
            ),
        ],
    )

    decision = broker.authorize_local_export_package(
        local_export_package_request(
            included_assets=(
                LocalExportPackageAsset("asset-primary-video", "primary_video"),
                LocalExportPackageAsset("asset-background-music", "music"),
            )
        )
    )

    assert decision.allowed is False
    assert decision.reason == "content_rights_status_denied"
    assert decision.asset_id == "asset-background-music"
    assert decision.rights_status == "denied"
    assert decision.path == "package-short-1"


def test_generic_authorize_blocks_local_export_package_for_missing_or_denied_asset_rights(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    missing = CapabilityBroker(workspace_root=workspace).authorize(
        local_export_package_request()
    )
    denied = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(
                rights_status="denied",
                allowed_operations=("include_in_export_package",),
            ),
        ],
    ).authorize(local_export_package_request())

    assert missing.allowed is False
    assert missing.reason == "content_rights_status_missing"
    assert missing.asset_id == "asset-primary-video"
    assert denied.allowed is False
    assert denied.reason == "content_rights_status_denied"
    assert denied.asset_id == "asset-primary-video"


def test_local_export_dispatch_blocks_generation_and_reports_failing_asset(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(
                allowed_operations=("include_in_export_package",),
            ),
            content_rights_receipt(
                asset_id="asset-background-music",
                rights_status="expired",
                allowed_operations=("include_in_export_package",),
                allowed_data_classes=("music",),
            ),
        ],
    )
    export_calls = []
    request_to_export = local_export_package_request(
        included_assets=(
            LocalExportPackageAsset("asset-primary-video", "primary_video"),
            LocalExportPackageAsset("asset-background-music", "music"),
        )
    )

    decision = broker.authorize_local_export_package(request_to_export)
    with pytest.raises(PermissionError, match="content_rights_status_expired"):
        broker.dispatch_local_export_package(
            request_to_export,
            lambda: export_calls.append("generated"),
        )

    assert decision.allowed is False
    assert decision.asset_id == "asset-background-music"
    assert decision.reason == "content_rights_status_expired"
    assert export_calls == []


def test_local_export_dispatch_generates_after_all_asset_rights_pass(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        content_rights_receipts=[
            content_rights_receipt(
                allowed_operations=("include_in_export_package",),
            ),
        ],
    )
    export_calls = []

    result = broker.dispatch_local_export_package(
        local_export_package_request(),
        lambda: export_calls.append("generated") or "package-short-1",
    )

    assert result == "package-short-1"
    assert export_calls == ["generated"]


def test_authorizes_non_secret_access_inside_configured_local_allowlist(tmp_path):
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "synced_media"
    workspace.mkdir()
    media_root.mkdir()
    policy = LocalScopePolicy(workspace_root=workspace, local_allowlist=[media_root])

    decision = policy.authorize(request(media_root / "lesson.mp4", data_class="primary_video"))

    assert decision.allowed is True
    assert decision.approved_scope == str(media_root.resolve())


def test_relative_local_allowlist_cannot_approve_process_directory(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    process_media_root = tmp_path / "synced_media"
    workspace.mkdir()
    process_media_root.mkdir()
    monkeypatch.chdir(tmp_path)
    policy = LocalScopePolicy(workspace_root=workspace, local_allowlist=["synced_media"])

    decision = policy.authorize(
        request(process_media_root / "lesson.mp4", data_class="primary_video")
    )

    assert decision.allowed is False
    assert decision.reason == "path_outside_approved_local_scopes"


def test_broker_authorizes_new_non_secret_local_data_classes_inside_scope(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)

    decision = broker.authorize_local_data_access(
        request("captions/track.vtt", data_class="caption_track")
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_non_secret_local_scope"


def test_denies_reads_and_writes_outside_workspace_or_allowlist(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    policy = LocalScopePolicy(workspace_root=workspace)

    read_decision = policy.authorize(request(outside / "transcript.json"))
    write_decision = policy.authorize(
        request("../outside/render.mp4", operation="write", data_class="render")
    )

    assert read_decision.allowed is False
    assert read_decision.reason == "path_outside_approved_local_scopes"
    assert write_decision.allowed is False
    assert write_decision.reason == "path_outside_approved_local_scopes"


def test_denies_secret_data_even_when_path_is_local(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = LocalScopePolicy(workspace_root=workspace)

    decision = policy.authorize(request("vault/token.txt", data_class="api_secret"))

    assert decision.allowed is False
    assert decision.reason == "secret_data_requires_vault_reference"


def secret_request(
    *,
    store_id: str = "creator-keychain",
    secret_ref: str = "vault://creator-1/provider-a/api-key",
    tool_id: str = "caption-provider-client",
    tool_digest: str | None = None,
    toolbox_path: Path | str | None = None,
    execution_principal: str = "local-tool:caption-provider-client",
    requester_id: str | None = "local-tool:caption-provider-client",
    session_id: str | None = "secret-session-1",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
):
    tool_digest = tool_digest if tool_digest is not None else TOOL_DIGESTS.get(tool_id)
    toolbox_path = toolbox_path if toolbox_path is not None else tool_path(tool_id)
    return SecretAccessRequest(
        operation="read_secret",
        store_id=store_id,
        secret_ref=secret_ref,
        tool_id=tool_id,
        execution_principal=execution_principal,
        agent_id="meaning-scout",
        project_id=project_id,
        creator_id=creator_id,
        tool_digest=tool_digest,
        toolbox_path=toolbox_path,
        requester_id=requester_id,
        session_id=session_id,
    )


def secret_store(
    *,
    store_id: str = "creator-keychain",
    source: str = "macos-keychain://reelbrain/creator-1",
):
    return ApprovedSecretStore(
        store_id=store_id,
        kind="macos_keychain",
        source=source,
    )


def secret_grant(
    *,
    store_id: str = "creator-keychain",
    secret_ref: str = "vault://creator-1/provider-a/api-key",
    tool_id: str = "caption-provider-client",
    execution_principal: str = "local-tool:caption-provider-client",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    revoked: bool = False,
):
    return SecretAccessGrant(
        store_id=store_id,
        secret_ref=secret_ref,
        tool_id=tool_id,
        execution_principal=execution_principal,
        project_id=project_id,
        creator_id=creator_id,
        revoked=revoked,
    )


def test_authorizes_secret_read_only_from_approved_store_for_matching_principal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant()],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_secret_access(secret_request())

    assert decision.allowed is True
    assert decision.reason == "authorized_secret_store_reference"
    assert decision.approved_scope == "macos_keychain:macos-keychain://reelbrain/creator-1"
    assert decision.secret_store_id == "creator-keychain"
    assert decision.secret_ref == "vault://creator-1/provider-a/api-key"
    assert decision.tool_id == "caption-provider-client"
    assert decision.execution_principal == "local-tool:caption-provider-client"
    assert decision.requester_id == "local-tool:caption-provider-client"
    assert decision.session_id == "secret-session-1"


def test_denies_secret_read_from_unapproved_store_even_with_matching_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store(store_id="approved-keychain")],
        secret_access_grants=[secret_grant(store_id="unapproved-keychain")],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_secret_access(secret_request(store_id="unapproved-keychain"))

    assert decision.allowed is False
    assert decision.reason == "secret_store_not_approved"
    assert decision.secret_store_id == "unapproved-keychain"


def test_denies_secret_read_when_store_id_maps_to_multiple_sources(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[
            secret_store(source="macos-keychain://reelbrain/creator-1"),
            secret_store(source="macos-keychain://other-service/creator-1"),
        ],
        secret_access_grants=[secret_grant()],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_secret_access(secret_request())

    assert decision.allowed is False
    assert decision.reason == "secret_store_source_ambiguous"


def test_denies_secret_read_without_matching_tool_or_execution_principal_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant()],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    wrong_tool = broker.authorize_secret_access(secret_request(tool_id="caption-api"))
    wrong_principal = broker.authorize_secret_access(
        secret_request(execution_principal="agent:meaning-scout")
    )

    assert wrong_tool.allowed is False
    assert wrong_tool.reason == "secret_access_grant_required"
    assert wrong_principal.allowed is False
    assert wrong_principal.reason == "secret_access_grant_required"


def test_denies_secret_read_for_wrong_creator_or_project_and_revoked_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant(revoked=True)],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    wrong_creator = broker.authorize_secret_access(secret_request(creator_id="creator-2"))
    wrong_project = broker.authorize_secret_access(secret_request(project_id="project-2"))
    revoked = broker.authorize_secret_access(secret_request())

    assert wrong_creator.allowed is False
    assert wrong_creator.reason == "secret_access_grant_required"
    assert wrong_project.allowed is False
    assert wrong_project.reason == "secret_access_grant_required"
    assert revoked.allowed is False
    assert revoked.reason == "secret_access_grant_revoked"


def test_revoked_secret_grant_overrides_duplicate_active_grant(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant(), secret_grant(revoked=True)],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_secret_access(secret_request())

    assert decision.allowed is False
    assert decision.reason == "secret_access_grant_revoked"


def test_denies_secret_read_without_active_matching_execution_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    no_session_broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant()],
        acp_registry=acp_registry(),
    )
    inactive_session_broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant()],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="local-tool:caption-provider-client",
                agent_id="meaning-scout",
                active=False,
            )
        ],
        acp_registry=acp_registry(),
    )
    mismatched_principal_broker = CapabilityBroker(
        workspace_root=workspace,
        secret_stores=[secret_store()],
        secret_access_grants=[secret_grant()],
        tool_sessions=[
            tool_session(
                session_id="secret-session-1",
                requester_id="agent:meaning-scout",
                agent_id="meaning-scout",
            )
        ],
        acp_registry=acp_registry(),
    )

    no_session = no_session_broker.authorize_secret_access(secret_request())
    inactive_session = inactive_session_broker.authorize_secret_access(secret_request())
    mismatched_principal = mismatched_principal_broker.authorize_secret_access(secret_request())
    missing_context_fields = inactive_session_broker.authorize_secret_access(
        secret_request(requester_id=None, session_id=None)
    )

    assert no_session.allowed is False
    assert no_session.reason == "secret_execution_context_required"
    assert inactive_session.allowed is False
    assert inactive_session.reason == "secret_execution_context_inactive"
    assert mismatched_principal.allowed is False
    assert mismatched_principal.reason == "secret_execution_context_required"
    assert missing_context_fields.allowed is False
    assert missing_context_fields.reason == "secret_execution_context_required"


def test_denies_symlink_escape_from_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    policy = LocalScopePolicy(workspace_root=workspace)

    decision = policy.authorize(request("external/transcript.json"))

    assert decision.allowed is False
    assert decision.reason == "path_outside_approved_local_scopes"


def test_denies_invalid_local_path_instead_of_bypassing_scope_check(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = LocalScopePolicy(workspace_root=workspace)

    decision = policy.authorize(request("invalid\0transcript.json"))

    assert decision.allowed is False
    assert decision.reason == "invalid_local_path"


def outbound_request(
    *,
    operation: str = "export",
    provider: str = "provider-a",
    url: str | None = None,
    host: str | None = None,
    bucket: str | None = None,
    endpoint: str | None = None,
    tool_id: str = "export-relay",
    tool_digest: str | None = None,
    toolbox_path: Path | str | None = None,
    invocation_id: str | None = "invocation-1",
):
    tool_digest = tool_digest if tool_digest is not None else TOOL_DIGESTS.get(tool_id)
    toolbox_path = toolbox_path if toolbox_path is not None else tool_path(tool_id)
    return OutboundDestinationRequest(
        operation=operation,
        provider=provider,
        url=url,
        host=host,
        bucket=bucket,
        endpoint=endpoint,
        agent_id="showrunner",
        project_id="project-1",
        creator_id="creator-1",
        tool_id=tool_id,
        tool_digest=tool_digest,
        toolbox_path=toolbox_path,
        invocation_id=invocation_id,
    )


def consent(
    *,
    provider: str = "provider-a",
    tool_id: str = "export-relay",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    invocation_id: str | None = "invocation-1",
    revoked: bool = False,
):
    return ProviderConsentReceipt(
        provider=provider,
        tool_id=tool_id,
        project_id=project_id,
        creator_id=creator_id,
        invocation_id=invocation_id,
        revoked=revoked,
    )


def tool_session(
    *,
    session_id: str = "session-1",
    requester_id: str = "agent:showrunner",
    agent_id: str = "showrunner",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    active: bool = True,
):
    return ToolExecutionSession(
        session_id=session_id,
        requester_id=requester_id,
        agent_id=agent_id,
        project_id=project_id,
        creator_id=creator_id,
        active=active,
    )


def tool_grant(
    *,
    capability: str = "render:short-form",
    tool_id: str = "export-relay",
    requester_id: str = "agent:showrunner",
    session_id: str = "session-1",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    revoked: bool = False,
):
    return ToolCapabilityGrant(
        capability=capability,
        tool_id=tool_id,
        requester_id=requester_id,
        session_id=session_id,
        project_id=project_id,
        creator_id=creator_id,
        revoked=revoked,
    )


def budget_reservation(
    *,
    reservation_id: str = "budget-1",
    requester_id: str = "agent:showrunner",
    session_id: str = "session-1",
    tool_id: str = "export-relay",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    capabilities: tuple[str, ...] = ("render:short-form",),
    reserved_amount_cents: int = 250,
    metered_units: int = 0,
    cost_authorization_receipt_id: str | None = None,
    state: str = "reserved",
    revoked: bool = False,
):
    return BudgetReservationReceipt(
        reservation_id=reservation_id,
        requester_id=requester_id,
        session_id=session_id,
        tool_id=tool_id,
        project_id=project_id,
        creator_id=creator_id,
        capabilities=capabilities,
        reserved_amount_cents=reserved_amount_cents,
        metered_units=metered_units,
        cost_authorization_receipt_id=cost_authorization_receipt_id,
        state=state,
        revoked=revoked,
    )


def tool_assignment_request(
    *,
    requester_id: str = "agent:showrunner",
    session_id: str = "session-1",
    agent_id: str = "showrunner",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    tool_id: str = "export-relay",
    tool_digest: str | None = None,
    toolbox_path: Path | str | None = None,
    capabilities: tuple[str, ...] = ("render:short-form",),
):
    tool_digest = tool_digest if tool_digest is not None else TOOL_DIGESTS.get(tool_id)
    toolbox_path = toolbox_path if toolbox_path is not None else tool_path(tool_id)
    return ToolAssignmentRequest(
        operation="assign_tool",
        requester_id=requester_id,
        session_id=session_id,
        agent_id=agent_id,
        project_id=project_id,
        creator_id=creator_id,
        tool_id=tool_id,
        tool_digest=tool_digest,
        toolbox_path=toolbox_path,
        capabilities=capabilities,
    )


def tool_approval_request(
    *,
    approver_id: str = "human:tool-auditor",
    agent_id: str = "tool-auditor",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    tool_id: str = "export-relay",
    tool_digest: str | None = None,
    toolbox_path: Path | str | None = None,
    human_confirmed: bool = False,
    approval_receipt_id: str | None = None,
):
    tool_digest = tool_digest if tool_digest is not None else TOOL_DIGESTS.get(tool_id)
    toolbox_path = toolbox_path if toolbox_path is not None else tool_path(tool_id)
    return ToolApprovalRequest(
        operation="approve_tool",
        approver_id=approver_id,
        agent_id=agent_id,
        project_id=project_id,
        creator_id=creator_id,
        tool_id=tool_id,
        tool_digest=tool_digest,
        toolbox_path=toolbox_path,
        human_confirmed=human_confirmed,
        approval_receipt_id=approval_receipt_id,
    )


def tool_execution_request(
    *,
    requester_id: str = "agent:showrunner",
    session_id: str = "session-1",
    agent_id: str = "showrunner",
    project_id: str = "project-1",
    creator_id: str = "creator-1",
    tool_id: str = "export-relay",
    tool_digest: str | None = None,
    toolbox_path: Path | str | None = None,
    capabilities: tuple[str, ...] = ("render:short-form",),
    paid: bool = False,
    metered: bool = False,
    budget_reservation_id: str | None = None,
    cost_authorization_receipt_id: str | None = None,
    provider: str | None = None,
    invocation_id: str | None = "invocation-1",
):
    tool_digest = tool_digest if tool_digest is not None else TOOL_DIGESTS.get(tool_id)
    toolbox_path = toolbox_path if toolbox_path is not None else tool_path(tool_id)
    return ToolExecutionRequest(
        operation="execute_tool",
        requester_id=requester_id,
        session_id=session_id,
        agent_id=agent_id,
        project_id=project_id,
        creator_id=creator_id,
        tool_id=tool_id,
        tool_digest=tool_digest,
        toolbox_path=toolbox_path,
        capabilities=capabilities,
        paid=paid,
        metered=metered,
        budget_reservation_id=budget_reservation_id,
        cost_authorization_receipt_id=cost_authorization_receipt_id,
        provider=provider,
        invocation_id=invocation_id,
    )


def test_quarantined_tool_is_auditable_but_not_assignable_approvable_or_executable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    quarantined_tool = approved_tool("export-relay", lifecycle="quarantined")
    registry = acp_registry(quarantined_tool)
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=registry,
    )
    dispatched = []

    audited_tool = registry.resolve_tool(
        tool_id="export-relay",
        digest=TOOL_DIGESTS["export-relay"],
        toolbox_path=tool_path("export-relay"),
    )
    assignment = broker.authorize_tool_assignment(tool_assignment_request())
    approval = broker.authorize_tool_approval(
        tool_approval_request(human_confirmed=True, approval_receipt_id="approval-1")
    )
    execution = broker.authorize_tool_execution(tool_execution_request())

    with pytest.raises(PermissionError, match="acp_tool_quarantined"):
        broker.dispatch_tool_execution(
            tool_execution_request(),
            lambda: dispatched.append("called"),
        )

    assert audited_tool == quarantined_tool
    assert registry.contains_tool_id("export-relay") is True
    assert registry.audit_tools() == (quarantined_tool,)
    assert registry.audit_tools(lifecycle="quarantined") == (quarantined_tool,)
    assert assignment.allowed is False
    assert assignment.reason == "acp_tool_quarantined"
    assert approval.allowed is False
    assert approval.reason == "acp_tool_quarantined"
    assert execution.allowed is False
    assert execution.reason == "acp_tool_quarantined"
    assert dispatched == []


def test_quarantined_generated_tool_requires_human_confirmation_before_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    generated_tool = approved_tool(
        "generated-editor",
        lifecycle="quarantined",
        origin="generated",
    )
    broker = CapabilityBroker(
        workspace_root=workspace,
        acp_registry=acp_registry(generated_tool),
    )

    non_human = broker.authorize_tool_approval(
        tool_approval_request(
            approver_id="agent:toolsmith",
            agent_id="toolsmith",
            tool_id="generated-editor",
            human_confirmed=True,
            approval_receipt_id="approval-1",
        )
    )
    unconfirmed = broker.authorize_tool_approval(
        tool_approval_request(
            tool_id="generated-editor",
            human_confirmed=False,
            approval_receipt_id="approval-1",
        )
    )
    missing_receipt = broker.authorize_tool_approval(
        tool_approval_request(
            tool_id="generated-editor",
            human_confirmed=True,
        )
    )
    confirmed = broker.authorize_tool_approval(
        tool_approval_request(
            tool_id="generated-editor",
            human_confirmed=True,
            approval_receipt_id="approval-1",
        )
    )

    assert non_human.allowed is False
    assert non_human.reason == "human_approval_required"
    assert unconfirmed.allowed is False
    assert unconfirmed.reason == "human_confirmation_required"
    assert missing_receipt.allowed is False
    assert missing_receipt.reason == "approval_receipt_required"
    assert confirmed.allowed is True
    assert confirmed.reason == "authorized_tool_approval"


def test_generated_tool_is_promoted_only_after_confirmation_and_then_can_execute(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    generated_tool = approved_tool(
        "generated-editor",
        lifecycle="quarantined",
        origin="generated",
    )
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(tool_id="generated-editor")],
        acp_registry=acp_registry(generated_tool),
    )
    approval_request = tool_approval_request(
        tool_id="generated-editor",
        human_confirmed=True,
        approval_receipt_id="approval-1",
    )

    approval = broker.authorize_tool_approval(approval_request)
    before_promotion = broker.authorize_tool_execution(
        tool_execution_request(tool_id="generated-editor")
    )
    original_tool = broker.acp_registry.resolve_tool(
        tool_id="generated-editor",
        digest=TOOL_DIGESTS["generated-editor"],
        toolbox_path=tool_path("generated-editor"),
    )

    promoted_registry = broker.promote_approved_tool(approval_request)
    promoted_tool = promoted_registry.resolve_tool(
        tool_id="generated-editor",
        digest=TOOL_DIGESTS["generated-editor"],
        toolbox_path=tool_path("generated-editor"),
    )
    promoted_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(tool_id="generated-editor")],
        acp_registry=promoted_registry,
    )
    after_promotion = promoted_broker.authorize_tool_execution(
        tool_execution_request(tool_id="generated-editor")
    )
    dispatched = []
    promoted_broker.dispatch_tool_execution(
        tool_execution_request(tool_id="generated-editor"),
        lambda: dispatched.append("called"),
    )

    with pytest.raises(ValueError, match="human_approval_required"):
        broker.acp_registry.promote_generated_tool(
            tool_id="generated-editor",
            digest=TOOL_DIGESTS["generated-editor"],
            toolbox_path=tool_path("generated-editor"),
            human_approval_receipt_id="approval-1",
        )

    assert approval.allowed is True
    assert approval.reason == "authorized_tool_approval"
    assert before_promotion.allowed is False
    assert before_promotion.reason == "acp_tool_quarantined"
    assert original_tool == generated_tool
    assert promoted_tool is not None
    assert promoted_tool.lifecycle == "approved"
    assert promoted_tool.human_approval_receipt_id == "approval-1"
    assert after_promotion.allowed is True
    assert after_promotion.reason == "authorized_tool_execution"
    assert dispatched == ["called"]


def test_generated_tool_cannot_execute_from_registry_without_human_approval_receipt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(tool_id="generated-editor")],
        acp_registry=acp_registry(
            approved_tool("generated-editor", lifecycle="approved", origin="generated")
        ),
    )

    decision = broker.authorize_tool_execution(tool_execution_request(tool_id="generated-editor"))

    assert decision.allowed is False
    assert decision.reason == "generated_tool_human_approval_required"


def test_authorizes_tool_execution_before_dispatch_for_matching_session_and_grants(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[
            tool_grant(capability="render:short-form"),
            tool_grant(capability="filesystem:write:exports"),
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []

    decision = broker.authorize_tool_execution(
        tool_execution_request(capabilities=("render:short-form", "filesystem:write:exports"))
    )
    result = broker.dispatch_tool_execution(
        tool_execution_request(capabilities=("render:short-form", "filesystem:write:exports")),
        lambda: dispatched.append("called") or "rendered",
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_tool_execution"
    assert decision.requester_id == "agent:showrunner"
    assert decision.session_id == "session-1"
    assert decision.tool_id == "export-relay"
    assert decision.capabilities == ("render:short-form", "filesystem:write:exports")
    assert result == "rendered"
    assert dispatched == ["called"]


def test_authorizes_network_backed_tool_dispatch_with_active_provider_tool_consent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        budget_reservations=[
            budget_reservation(
                capabilities=("network:caption-provider",),
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        provider_consents=[consent(provider="provider-a", tool_id="export-relay")],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
        provider="provider-a",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)
    result = broker.dispatch_tool_execution(
        request,
        lambda: dispatched.append("called") or "provider-result",
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_tool_execution"
    assert decision.provider == "provider-a"
    assert result == "provider-result"
    assert dispatched == ["called"]


@pytest.mark.parametrize("invocation_id", [None, "invocation-2"])
def test_denies_network_backed_tool_dispatch_without_consent_for_that_invocation(
    tmp_path,
    invocation_id,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        budget_reservations=[
            budget_reservation(
                capabilities=("network:caption-provider",),
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        provider_consents=[
            consent(
                provider="provider-a",
                tool_id="export-relay",
                invocation_id="invocation-1",
            )
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
        provider="provider-a",
        invocation_id=invocation_id,
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="provider_consent_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_consent_required"
    assert decision.invocation_id == invocation_id
    assert dispatched == []


def test_revoked_consent_overrides_active_consent_for_same_network_tool_invocation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        budget_reservations=[
            budget_reservation(
                capabilities=("network:caption-provider",),
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        provider_consents=[consent(), consent(revoked=True)],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
        provider="provider-a",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="provider_consent_revoked"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_consent_revoked"
    assert dispatched == []


def test_denies_paid_provider_tool_dispatch_without_budget_before_provider_consent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        provider="provider-a",
    )
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        provider_consents=[consent(provider="provider-a", tool_id="export-relay")],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="budget_reservation_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "budget_reservation_required"
    assert decision.provider == "provider-a"
    assert dispatched == []


def test_denies_network_backed_tool_dispatch_without_provider_after_budget_reservation(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
    )
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        budget_reservations=[
            budget_reservation(
                capabilities=("network:caption-provider",),
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="provider_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_required"
    assert dispatched == []


def test_denies_network_backed_tool_dispatch_without_provider_consent_after_budget_reservation(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = tool_execution_request(
        capabilities=("network:caption-provider",),
        provider="provider-a",
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
    )
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="network:caption-provider")],
        budget_reservations=[
            budget_reservation(
                capabilities=("network:caption-provider",),
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )

    decision = broker.authorize_tool_execution(request)

    assert decision.allowed is False
    assert decision.reason == "provider_consent_required"
    assert decision.provider == "provider-a"


def test_denies_explicit_paid_tool_dispatch_without_budget_reservation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(paid=True)
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="budget_reservation_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "budget_reservation_required"
    assert dispatched == []


def test_denies_provider_tool_dispatch_when_consent_is_revoked(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[
            budget_reservation(cost_authorization_receipt_id="cost-auth-1")
        ],
        provider_consents=[consent(provider="provider-a", tool_id="export-relay", revoked=True)],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        provider="provider-a",
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="provider_consent_revoked"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_consent_revoked"
    assert decision.provider == "provider-a"
    assert decision.tool_id == "export-relay"
    assert dispatched == []


def test_denies_metered_tool_dispatch_without_budget_reservation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []
    request = tool_execution_request(metered=True)

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="budget_reservation_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "budget_reservation_required"
    assert dispatched == []


def test_authorizes_metered_tool_dispatch_with_matching_active_budget_reservation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[
            tool_grant(capability="render:short-form"),
            tool_grant(capability="filesystem:write:exports"),
        ],
        budget_reservations=[
            budget_reservation(
                capabilities=("render:short-form", "filesystem:write:exports"),
                reserved_amount_cents=500,
                cost_authorization_receipt_id="cost-auth-1",
            )
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        capabilities=("render:short-form", "filesystem:write:exports"),
        metered=True,
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)
    result = broker.dispatch_tool_execution(
        request,
        lambda: dispatched.append("called") or "rendered",
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_tool_execution"
    assert decision.budget_reservation_id == "budget-1"
    assert result == "rendered"
    assert dispatched == ["called"]


def test_denies_metered_tool_dispatch_without_explicit_request_cost_authorization(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[
            budget_reservation(cost_authorization_receipt_id="cost-auth-1")
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(metered=True, budget_reservation_id="budget-1")
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="cost_authorization_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "cost_authorization_required"
    assert decision.budget_reservation_id == "budget-1"
    assert dispatched == []


@pytest.mark.parametrize("cost_mode", ["paid", "metered"])
def test_denies_costed_tool_dispatch_when_valid_reservation_lacks_cost_authorization(
    tmp_path,
    cost_mode,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[budget_reservation()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        **{cost_mode: True},
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="cost_authorization_required"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "cost_authorization_required"
    assert decision.budget_reservation_id == "budget-1"
    assert dispatched == []


def test_denies_paid_tool_dispatch_when_request_cost_authorization_mismatches_reservation(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[
            budget_reservation(cost_authorization_receipt_id="cost-auth-1")
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        paid=True,
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-2",
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match="cost_authorization_scope_mismatch"):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "cost_authorization_scope_mismatch"
    assert decision.budget_reservation_id == "budget-1"
    assert dispatched == []


def test_denies_metered_tool_execution_for_invalid_budget_reservations(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    inactive = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[budget_reservation(state="consumed")],
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_tool_execution(tool_execution_request(metered=True, budget_reservation_id="budget-1"))
    mismatched = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[budget_reservation(tool_id="caption-api")],
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_tool_execution(tool_execution_request(metered=True, budget_reservation_id="budget-1"))
    no_amount = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[budget_reservation(reserved_amount_cents=0, metered_units=0)],
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_tool_execution(tool_execution_request(metered=True, budget_reservation_id="budget-1"))

    assert inactive.allowed is False
    assert inactive.reason == "budget_reservation_inactive"
    assert mismatched.allowed is False
    assert mismatched.reason == "budget_reservation_scope_mismatch"
    assert no_amount.allowed is False
    assert no_amount.reason == "budget_reservation_amount_required"


@pytest.mark.parametrize(
    ("request_overrides", "reservation_overrides", "expected_reason"),
    [
        (
            {"paid": True},
            {"reserved_amount_cents": 0, "metered_units": 10},
            "budget_reservation_amount_required",
        ),
        (
            {"metered": True},
            {"reserved_amount_cents": -1, "metered_units": 10},
            "budget_reservation_amount_invalid",
        ),
        (
            {"metered": True},
            {"reserved_amount_cents": 100, "metered_units": -1},
            "budget_reservation_amount_invalid",
        ),
    ],
)
def test_invalid_budget_amounts_block_paid_or_metered_dispatch(
    tmp_path,
    request_overrides,
    reservation_overrides,
    expected_reason,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        budget_reservations=[
            budget_reservation(
                cost_authorization_receipt_id="cost-auth-1",
                **reservation_overrides,
            )
        ],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    request = tool_execution_request(
        budget_reservation_id="budget-1",
        cost_authorization_receipt_id="cost-auth-1",
        **request_overrides,
    )
    dispatched = []

    decision = broker.authorize_tool_execution(request)

    with pytest.raises(PermissionError, match=expected_reason):
        broker.dispatch_tool_execution(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == expected_reason
    assert dispatched == []


def test_denies_tool_dispatch_when_requester_or_session_is_not_active(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session(active=False)],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []

    inactive = broker.authorize_tool_execution(tool_execution_request())
    wrong_requester = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session(requester_id="agent:creator-advocate")],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_tool_execution(tool_execution_request())

    with pytest.raises(PermissionError, match="tool_session_inactive"):
        broker.dispatch_tool_execution(
            tool_execution_request(),
            lambda: dispatched.append("called"),
        )

    assert inactive.allowed is False
    assert inactive.reason == "tool_session_inactive"
    assert wrong_requester.allowed is False
    assert wrong_requester.reason == "tool_session_required"
    assert dispatched == []


def test_denies_tool_dispatch_for_unapproved_toolbox_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )

    bad_digest = broker.authorize_tool_execution(
        tool_execution_request(tool_digest="sha256:tampered")
    )
    bad_toolbox_path = broker.authorize_tool_execution(
        tool_execution_request(toolbox_path="~/.ReelBrain/toolbox/runtime/export-relay")
    )

    assert bad_digest.allowed is False
    assert bad_digest.reason == "acp_tool_identity_mismatch"
    assert bad_toolbox_path.allowed is False
    assert bad_toolbox_path.reason == "acp_tool_identity_mismatch"


def test_denies_tool_dispatch_without_matching_capability_grants(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(capability="render:short-form", revoked=True)],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    dispatched = []

    revoked = broker.authorize_tool_execution(tool_execution_request())
    missing = broker.authorize_tool_execution(
        tool_execution_request(capabilities=("filesystem:write:exports",))
    )

    with pytest.raises(PermissionError, match="tool_capability_grant_revoked"):
        broker.dispatch_tool_execution(
            tool_execution_request(),
            lambda: dispatched.append("called"),
        )

    assert revoked.allowed is False
    assert revoked.reason == "tool_capability_grant_revoked"
    assert revoked.capabilities == ("render:short-form",)
    assert missing.allowed is False
    assert missing.reason == "tool_capability_grant_required"
    assert missing.capabilities == ("filesystem:write:exports",)
    assert dispatched == []


def test_revoked_session_and_grant_override_conflicting_active_records_before_dispatch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dispatched = []
    request = tool_execution_request()

    session_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session(), tool_session(active=False)],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    grant_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant(), tool_grant(revoked=True)],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )

    with pytest.raises(PermissionError, match="tool_session_inactive"):
        session_broker.dispatch_tool_execution(
            request,
            lambda: dispatched.append("conflicting-session"),
        )
    with pytest.raises(PermissionError, match="tool_capability_grant_revoked"):
        grant_broker.dispatch_tool_execution(
            request,
            lambda: dispatched.append("conflicting-grant"),
        )

    assert dispatched == []


def test_tool_dispatch_checks_requester_session_toolbox_and_grants_before_call(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dispatch_calls = []

    inactive_session_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session(active=False)],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    tampered_toolbox_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )
    missing_grant_broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[],
        acp_registry=acp_registry(approved_tool("export-relay")),
    )

    with pytest.raises(PermissionError, match="tool_session_inactive"):
        inactive_session_broker.dispatch_tool_execution(
            tool_execution_request(),
            lambda: dispatch_calls.append("inactive-session"),
        )
    with pytest.raises(PermissionError, match="acp_tool_identity_mismatch"):
        tampered_toolbox_broker.dispatch_tool_execution(
            tool_execution_request(tool_digest="sha256:tampered"),
            lambda: dispatch_calls.append("tampered-toolbox"),
        )
    with pytest.raises(PermissionError, match="tool_capability_grant_required"):
        missing_grant_broker.dispatch_tool_execution(
            tool_execution_request(),
            lambda: dispatch_calls.append("missing-grant"),
        )

    assert dispatch_calls == []


def test_acp_registry_is_sole_authority_for_tool_identity_and_toolbox_membership(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowlist = [DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload")]
    consents = [consent(provider="provider-a", tool_id="export-relay")]
    request_to_allowed_destination = outbound_request(
        url="https://exports.reelbrain.local/upload"
    )

    missing_registry = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=allowlist,
        provider_consents=consents,
    )
    bad_digest = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=allowlist,
        provider_consents=consents,
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_outbound_destination(
        outbound_request(
            url="https://exports.reelbrain.local/upload",
            tool_digest="sha256:tampered",
        )
    )
    bad_toolbox_path = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=allowlist,
        provider_consents=consents,
        acp_registry=acp_registry(approved_tool("export-relay")),
    ).authorize_outbound_destination(
        outbound_request(
            url="https://exports.reelbrain.local/upload",
            toolbox_path="~/.ReelBrain/toolbox/runtime-injected/export-relay",
        )
    )
    disabled_tool = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=allowlist,
        provider_consents=consents,
        acp_registry=acp_registry(approved_tool("export-relay", lifecycle="disabled")),
    ).authorize_outbound_destination(request_to_allowed_destination)

    no_acp_decision = missing_registry.authorize_outbound_destination(
        request_to_allowed_destination
    )

    assert no_acp_decision.allowed is False
    assert no_acp_decision.reason == "acp_tool_not_registered"
    assert bad_digest.allowed is False
    assert bad_digest.reason == "acp_tool_identity_mismatch"
    assert bad_toolbox_path.allowed is False
    assert bad_toolbox_path.reason == "acp_tool_identity_mismatch"
    assert disabled_tool.allowed is False
    assert disabled_tool.reason == "acp_tool_not_approved"


def test_acp_toolbox_snapshot_is_immutable_and_copied_at_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = [approved_tool("export-relay")]
    snapshot = ACPRegistrySnapshot(tools)
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[consent(provider="provider-a", tool_id="export-relay")],
        acp_registry=snapshot,
    )

    tools.clear()
    decision = broker.authorize_outbound_destination(
        outbound_request(url="https://exports.reelbrain.local/upload")
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_outbound_destination"
    with pytest.raises(FrozenInstanceError):
        snapshot.tools += (approved_tool("cdn-relay"),)
    with pytest.raises(FrozenInstanceError):
        snapshot.tools[0].lifecycle = "revoked"
    with pytest.raises(AttributeError):
        snapshot.tools.append(approved_tool("cdn-relay"))


def test_acp_toolbox_snapshot_defensively_copies_tool_identities_at_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    externally_retained_tool = approved_tool("export-relay", lifecycle="quarantined")
    snapshot = ACPRegistrySnapshot([externally_retained_tool])
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=snapshot,
    )

    object.__setattr__(externally_retained_tool, "lifecycle", "approved")
    decision = broker.authorize_tool_execution(tool_execution_request())
    audited_tool = snapshot.audit_tools()[0]

    assert decision.allowed is False
    assert decision.reason == "acp_tool_quarantined"
    assert audited_tool is not externally_retained_tool
    assert audited_tool.lifecycle == "quarantined"


def test_capability_broker_owns_a_read_only_copy_of_the_acp_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_snapshot = acp_registry(approved_tool("export-relay"))
    broker = CapabilityBroker(
        workspace_root=workspace,
        tool_sessions=[tool_session()],
        tool_capability_grants=[tool_grant()],
        acp_registry=source_snapshot,
    )

    object.__setattr__(
        source_snapshot,
        "tools",
        (approved_tool("export-relay", lifecycle="revoked"),),
    )

    decision = broker.authorize_tool_execution(tool_execution_request())

    assert broker.acp_registry is not source_snapshot
    assert decision.allowed is True
    assert decision.reason == "authorized_tool_execution"
    with pytest.raises(AttributeError):
        broker.acp_registry = acp_registry(approved_tool("export-relay", lifecycle="revoked"))


def test_authorizes_outbound_destination_with_active_provider_tool_consent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
            DestinationAllowlistEntry("host", "cdn.reelbrain.local"),
        ],
        provider_consents=[
            consent(provider="provider-a", tool_id="export-relay"),
            consent(provider="provider-b", tool_id="cdn-relay"),
        ],
        acp_registry=acp_registry(),
    )

    provider_a_decision = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-a",
            tool_id="export-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )
    provider_b_decision = broker.authorize_outbound_destination(
        outbound_request(
            operation="transmit",
            provider="provider-b",
            tool_id="cdn-relay",
            host="cdn.reelbrain.local",
        )
    )

    assert provider_a_decision.allowed is True
    assert provider_a_decision.reason == "authorized_outbound_destination"
    assert provider_a_decision.provider == "provider-a"
    assert provider_a_decision.tool_id == "export-relay"
    assert provider_b_decision.allowed is True
    assert provider_b_decision.provider == "provider-b"
    assert provider_b_decision.tool_id == "cdn-relay"


@pytest.mark.parametrize("invocation_id", [None, "invocation-2"])
def test_denies_outbound_dispatch_without_consent_for_that_invocation(
    tmp_path,
    invocation_id,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[consent(invocation_id="invocation-1")],
        acp_registry=acp_registry(),
    )
    request = outbound_request(
        url="https://exports.reelbrain.local/upload",
        invocation_id=invocation_id,
    )
    dispatched = []

    decision = broker.authorize_outbound_destination(request)

    with pytest.raises(PermissionError, match="provider_consent_required"):
        broker.dispatch_outbound_destination(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_consent_required"
    assert decision.invocation_id == invocation_id
    assert dispatched == []


def test_revoked_consent_overrides_active_consent_for_same_outbound_invocation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[consent(), consent(revoked=True)],
        acp_registry=acp_registry(),
    )
    request = outbound_request(url="https://exports.reelbrain.local/upload")
    dispatched = []

    decision = broker.authorize_outbound_destination(request)

    with pytest.raises(PermissionError, match="provider_consent_revoked"):
        broker.dispatch_outbound_destination(request, lambda: dispatched.append("called"))

    assert decision.allowed is False
    assert decision.reason == "provider_consent_revoked"
    assert dispatched == []


def test_authorizes_same_allowlisted_destination_for_different_providers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[
            consent(provider="provider-a", tool_id="export-relay"),
            consent(provider="provider-b", tool_id="cdn-relay"),
        ],
        acp_registry=acp_registry(),
    )

    provider_a_decision = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-a",
            tool_id="export-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )
    provider_b_decision = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-b",
            tool_id="cdn-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )

    assert provider_a_decision.allowed is True
    assert provider_a_decision.destination == "url:https://exports.reelbrain.local/upload"
    assert provider_b_decision.allowed is True
    assert provider_b_decision.destination == "url:https://exports.reelbrain.local/upload"


def test_denies_outbound_provider_execution_without_matching_active_consent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[
            consent(provider="provider-a", tool_id="caption-api"),
            consent(provider="provider-b", tool_id="export-relay"),
        ],
        acp_registry=acp_registry(),
    )

    missing_provider_consent = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-c",
            tool_id="export-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )
    missing_tool_consent = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-a",
            tool_id="export-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )

    assert missing_provider_consent.allowed is False
    assert missing_provider_consent.reason == "provider_consent_required"
    assert missing_provider_consent.provider == "provider-c"
    assert missing_provider_consent.tool_id == "export-relay"
    assert missing_tool_consent.allowed is False
    assert missing_tool_consent.reason == "provider_consent_required"


def test_denies_outbound_provider_execution_when_consent_is_revoked(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[
            consent(provider="provider-a", tool_id="export-relay", revoked=True),
        ],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_outbound_destination(
        outbound_request(
            provider="provider-a",
            tool_id="export-relay",
            url="https://exports.reelbrain.local/upload",
        )
    )

    assert decision.allowed is False
    assert decision.reason == "provider_consent_revoked"
    assert decision.provider == "provider-a"
    assert decision.tool_id == "export-relay"


def test_denies_outbound_destination_when_url_host_bucket_or_endpoint_not_allowlisted(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
            DestinationAllowlistEntry("host", "cdn.reelbrain.local"),
            DestinationAllowlistEntry("bucket", "approved-export-bucket"),
            DestinationAllowlistEntry("endpoint", "local-render-relay"),
        ],
        provider_consents=[consent()],
        acp_registry=acp_registry(),
    )

    denied_url = broker.authorize_outbound_destination(
        outbound_request(url="https://unexpected.example/upload")
    )
    denied_host = broker.authorize_outbound_destination(outbound_request(host="evil.example"))
    denied_bucket = broker.authorize_outbound_destination(
        outbound_request(bucket="unapproved-export-bucket")
    )
    denied_endpoint = broker.authorize_outbound_destination(
        outbound_request(operation="transmit", endpoint="unapproved-relay")
    )

    assert denied_url.allowed is False
    assert denied_url.reason == "destination_not_allowlisted"
    assert denied_url.destination == "url:https://unexpected.example/upload"
    assert denied_host.allowed is False
    assert denied_host.reason == "destination_not_allowlisted"
    assert denied_bucket.allowed is False
    assert denied_bucket.reason == "destination_not_allowlisted"
    assert denied_endpoint.allowed is False
    assert denied_endpoint.reason == "destination_not_allowlisted"


def test_denies_unallowlisted_destination_before_provider_or_tool_scope(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[],
        acp_registry=acp_registry(),
    )

    decision = broker.authorize_outbound_destination(
        outbound_request(
            provider="unapproved-provider",
            tool_id="export-relay",
            url="https://unexpected.example/upload",
        )
    )

    assert decision.allowed is False
    assert decision.reason == "destination_not_allowlisted"
    assert decision.provider == "unapproved-provider"
    assert decision.tool_id == "export-relay"
    assert decision.destination == "url:https://unexpected.example/upload"


@pytest.mark.parametrize("operation", ["export", "transmit"])
def test_outbound_dispatch_blocks_unallowlisted_destination_before_side_effect(
    tmp_path,
    operation,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[consent()],
        acp_registry=acp_registry(),
    )
    dispatch_calls = []

    with pytest.raises(PermissionError, match="destination_not_allowlisted"):
        broker.dispatch_outbound_destination(
            outbound_request(
                operation=operation,
                url="https://unexpected.example/upload",
            ),
            lambda: dispatch_calls.append(operation),
        )

    assert dispatch_calls == []


def test_outbound_dispatch_runs_after_destination_and_provider_authorization(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(
        workspace_root=workspace,
        destination_allowlist=[
            DestinationAllowlistEntry("url", "https://exports.reelbrain.local/upload"),
        ],
        provider_consents=[consent()],
        acp_registry=acp_registry(),
    )

    result = broker.dispatch_outbound_destination(
        outbound_request(url="https://exports.reelbrain.local/upload"),
        lambda: "exported",
    )

    assert result == "exported"


def test_denies_outbound_request_without_declared_destination(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)

    decision = broker.authorize_outbound_destination(outbound_request())

    assert decision.allowed is False
    assert decision.reason == "outbound_destination_required"


def test_redacts_secret_material_from_logs_and_artifacts_before_completion(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)

    log_decision = broker.contain_payload_secrets(
        PayloadContainmentRequest(
            surface="log",
            payload={
                "message": "provider failed with sk-live-123",
                "nested": {"authorization": "Bearer sk-live-123"},
            },
            known_secret_values=("sk-live-123",),
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        )
    )
    artifact_decision = broker.contain_payload_secrets(
        PayloadContainmentRequest(
            surface="artifact",
            payload={"caption_manifest": [{"api_key": SecretValue("sk-live-123")}]},
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        )
    )

    assert log_decision.allowed is True
    assert log_decision.reason == "secret_material_redacted"
    assert log_decision.sanitized_payload == {
        "message": "provider failed with [REDACTED_SECRET]",
        "nested": {"authorization": "[REDACTED_SECRET]"},
    }
    assert log_decision.secret_paths == ("$.message", "$.nested.authorization")
    assert artifact_decision.allowed is True
    assert artifact_decision.reason == "secret_material_redacted"
    assert artifact_decision.sanitized_payload == {
        "caption_manifest": [{"api_key": "[REDACTED_SECRET]"}],
    }
    assert artifact_decision.secret_paths == ("$.caption_manifest[0].api_key",)


def test_blocks_secrets_in_non_secret_payload_fields_and_outbound_bodies(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)

    non_secret_field = broker.contain_payload_secrets(
        PayloadContainmentRequest(
            surface="non_secret_field",
            payload={"provider_options": {"access_token": "sk-live-123"}},
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        )
    )
    outbound_body = broker.contain_payload_secrets(
        PayloadContainmentRequest(
            surface="outbound_body",
            payload=b'{"text":"caption this","token":"sk-live-123"}',
            known_secret_values=(b"sk-live-123",),
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        )
    )

    assert non_secret_field.allowed is False
    assert non_secret_field.reason == "secret_material_blocked_from_non_secret_payload"
    assert non_secret_field.sanitized_payload is None
    assert non_secret_field.secret_paths == ("$.provider_options.access_token",)
    assert outbound_body.allowed is False
    assert outbound_body.reason == "secret_material_blocked_from_non_secret_payload"
    assert outbound_body.sanitized_payload is None
    assert outbound_body.secret_paths == ("$",)


def test_allows_secret_material_only_on_secret_typed_channel(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)
    payload = {"resolved_secret": SecretValue("sk-live-123")}

    decision = broker.contain_payload_secrets(
        PayloadContainmentRequest(
            surface="secret_channel",
            payload=payload,
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        )
    )

    assert decision.allowed is True
    assert decision.reason == "authorized_secret_typed_channel"
    assert decision.sanitized_payload == payload
    assert decision.secret_paths == ()


@pytest.mark.parametrize("surface", ["log", "artifact"])
def test_payload_dispatch_redacts_secrets_before_log_or_artifact_side_effect(
    tmp_path,
    surface,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)
    dispatched_payloads = []

    result = broker.dispatch_payload(
        PayloadContainmentRequest(
            surface=surface,
            payload={
                "message": "provider rejected sk-live-123",
                "authorization": SecretValue("sk-live-123"),
            },
            known_secret_values=("sk-live-123",),
            agent_id="showrunner",
            project_id="project-1",
            creator_id="creator-1",
        ),
        lambda payload: dispatched_payloads.append(payload) or "completed",
    )

    assert result == "completed"
    assert dispatched_payloads == [
        {
            "message": "provider rejected [REDACTED_SECRET]",
            "authorization": "[REDACTED_SECRET]",
        }
    ]


@pytest.mark.parametrize("surface", ["non_secret_field", "outbound_body"])
def test_payload_dispatch_blocks_secret_before_non_secret_or_outbound_side_effect(
    tmp_path,
    surface,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)
    dispatched_payloads = []

    with pytest.raises(
        PermissionError,
        match="secret_material_blocked_from_non_secret_payload",
    ):
        broker.dispatch_payload(
            PayloadContainmentRequest(
                surface=surface,
                payload={"body": SecretValue("sk-live-123")},
                agent_id="showrunner",
                project_id="project-1",
                creator_id="creator-1",
            ),
            dispatched_payloads.append,
        )

    assert dispatched_payloads == []


def test_payload_containment_receipts_do_not_repr_secret_material(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = CapabilityBroker(workspace_root=workspace)
    request = PayloadContainmentRequest(
        surface="secret_channel",
        payload={"credential": SecretValue("sk-live-123")},
        known_secret_values=("sk-live-123",),
        agent_id="showrunner",
        project_id="project-1",
        creator_id="creator-1",
    )

    decision = broker.contain_payload_secrets(request)

    assert "sk-live-123" not in repr(request)
    assert "sk-live-123" not in repr(decision)
