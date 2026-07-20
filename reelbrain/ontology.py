"""Explicit, serializable records for the ReelBrain v1 ontology.

These records describe durable project facts. They intentionally keep secret
material and deleted creator content out of the ontology surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from .lifecycle import RunState, utc_now


def _require(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name}_required")


@dataclass(frozen=True)
class TranscriptRecord:
    record_id: str
    creator_id: str
    project_id: str
    source_start: float
    source_end: float
    language: str
    text: str
    confidence: float
    correction_history: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    version: int = 1
    deleted: bool = False

    def __post_init__(self) -> None:
        for name in ("record_id", "creator_id", "project_id", "language", "text"):
            _require(getattr(self, name), name)
        if self.source_start < 0 or self.source_end <= self.source_start:
            raise ValueError("invalid_transcript_timing")
        if not 0 <= self.confidence <= 1:
            raise ValueError("invalid_transcript_confidence")
        if self.version < 1:
            raise ValueError("invalid_transcript_version")


@dataclass(frozen=True)
class CaptionRecord:
    record_id: str
    creator_id: str
    project_id: str
    transcript_record_ids: tuple[str, ...]
    output_artifact_id: str
    start: float
    end: float
    rendered_text: str
    style_reference: str
    validation_results: tuple[str, ...] = ()
    correction_history: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    version: int = 1
    deleted: bool = False

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "creator_id",
            "project_id",
            "output_artifact_id",
            "rendered_text",
            "style_reference",
        ):
            _require(getattr(self, name), name)
        if not self.transcript_record_ids:
            raise ValueError("caption_transcript_link_required")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("invalid_caption_timing")
        if len(self.rendered_text.splitlines()) > 2:
            raise ValueError("caption_exceeds_two_lines")
        if self.version < 1:
            raise ValueError("invalid_caption_version")


ThumbnailRightsState = Literal["approved", "denied", "expired", "incompatible"]
ThumbnailApprovalState = Literal["draft", "creator_approved", "rejected", "revoked"]


@dataclass(frozen=True)
class ThumbnailRecord:
    record_id: str
    creator_id: str
    project_id: str
    run_id: str
    artifact_path: str
    provenance: tuple[str, ...]
    rights_state: ThumbnailRightsState
    approval_state: ThumbnailApprovalState
    artifact_sha256: str
    version: int = 1
    deleted: bool = False

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "creator_id",
            "project_id",
            "run_id",
            "artifact_path",
            "artifact_sha256",
        ):
            _require(getattr(self, name), name)
        if self.version < 1:
            raise ValueError("invalid_thumbnail_version")


@dataclass(frozen=True)
class ProviderConsentRecord:
    record_id: str
    creator_id: str
    project_id: str
    provider: str
    destination: str
    disclosed_data_categories: tuple[str, ...]
    purpose: str
    expected_retention: str
    expected_cost: float
    approved_at: str
    associated_call_ids: tuple[str, ...] = ()
    expires_at: str | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "creator_id",
            "project_id",
            "provider",
            "destination",
            "purpose",
            "expected_retention",
            "approved_at",
        ):
            _require(getattr(self, name), name)
        if not self.disclosed_data_categories:
            raise ValueError("provider_data_categories_required")
        if self.expected_cost < 0:
            raise ValueError("invalid_expected_provider_cost")


VaultStoreKind = Literal["macos_keychain", "encrypted_vault"]


@dataclass(frozen=True)
class VaultReferenceRecord:
    record_id: str
    creator_id: str
    provider: str
    store_kind: VaultStoreKind
    opaque_reference: str
    permitted_tool_ids: tuple[str, ...]
    expires_at: str | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        for name in ("record_id", "creator_id", "provider", "opaque_reference"):
            _require(getattr(self, name), name)
        allowed_prefixes = ("keychain://", "vault://")
        if not self.opaque_reference.startswith(allowed_prefixes):
            raise ValueError("vault_reference_must_be_opaque")
        if not self.permitted_tool_ids:
            raise ValueError("vault_permitted_tools_required")


@dataclass(frozen=True)
class RunStateRecord:
    record_id: str
    run_id: str
    project_id: str
    epoch: int
    previous: RunState
    current: RunState
    actor_identity_id: str
    reason: str
    evidence: tuple[str, ...] = ()
    occurred_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "run_id",
            "project_id",
            "actor_identity_id",
            "reason",
            "occurred_at",
        ):
            _require(getattr(self, name), name)
        if self.epoch < 1:
            raise ValueError("invalid_run_epoch")
        if self.previous == self.current:
            raise ValueError("run_state_transition_required")


FencePropagationState = Literal["pending", "propagated", "verified", "failed"]


@dataclass(frozen=True)
class DeletionTombstoneRecord:
    record_id: str
    creator_id: str
    subject_type: str
    subject_id: str
    subject_digest: str
    scope: str
    deletion_receipt_id: str
    fence_propagation_state: FencePropagationState
    deleted_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "creator_id",
            "subject_type",
            "subject_id",
            "subject_digest",
            "scope",
            "deletion_receipt_id",
            "deleted_at",
        ):
            _require(getattr(self, name), name)


AgentLifecycleState = Literal["active", "quarantined", "disabled", "revoked"]


@dataclass(frozen=True)
class AgentIdentityRecord:
    record_id: str
    agent_id: str
    role: str
    version: str
    allowed_acp_surfaces: tuple[str, ...]
    capability_subject: str
    configuration_digest: str
    lifecycle_state: AgentLifecycleState = "active"

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "agent_id",
            "role",
            "version",
            "capability_subject",
            "configuration_digest",
        ):
            _require(getattr(self, name), name)


@dataclass(frozen=True)
class ReelBrainOntologyRecords:
    transcripts: tuple[TranscriptRecord, ...] = ()
    captions: tuple[CaptionRecord, ...] = ()
    thumbnails: tuple[ThumbnailRecord, ...] = ()
    provider_consents: tuple[ProviderConsentRecord, ...] = ()
    vault_references: tuple[VaultReferenceRecord, ...] = ()
    run_states: tuple[RunStateRecord, ...] = ()
    deletion_tombstones: tuple[DeletionTombstoneRecord, ...] = ()
    agent_identities: tuple[AgentIdentityRecord, ...] = ()

    def to_document(self) -> dict[str, object]:
        """Return a JSON-serializable ontology document."""

        return asdict(self)
