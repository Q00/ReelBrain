from dataclasses import fields

import pytest

from reelbrain.lifecycle import RunState
from reelbrain.ontology import (
    AgentIdentityRecord,
    CaptionRecord,
    DeletionTombstoneRecord,
    ProviderConsentRecord,
    ReelBrainOntologyRecords,
    RunStateRecord,
    ThumbnailRecord,
    TranscriptRecord,
    VaultReferenceRecord,
)


def test_ontology_exposes_all_required_record_collections_without_secret_content():
    transcript = TranscriptRecord(
        record_id="transcript-1",
        creator_id="creator-1",
        project_id="project-1",
        source_start=1.0,
        source_end=4.0,
        language="en",
        text="Memory is a behavioral prior.",
        confidence=0.99,
        provenance=("local-whisper",),
    )
    caption = CaptionRecord(
        record_id="caption-1",
        creator_id="creator-1",
        project_id="project-1",
        transcript_record_ids=(transcript.record_id,),
        output_artifact_id="short-1",
        start=0.0,
        end=3.0,
        rendered_text="Memory is a\nbehavioral prior.",
        style_reference="creator-style-1",
        validation_results=("wer>=0.95", "two-lines-max"),
    )
    thumbnail = ThumbnailRecord(
        record_id="thumbnail-1",
        creator_id="creator-1",
        project_id="project-1",
        run_id="run-1",
        artifact_path="artifacts/thumbnail.png",
        provenance=("openai:gpt-image-2",),
        rights_state="approved",
        approval_state="creator_approved",
        artifact_sha256="abc123",
    )
    provider_consent = ProviderConsentRecord(
        record_id="consent-1",
        creator_id="creator-1",
        project_id="project-1",
        provider="openai",
        destination="api.openai.com",
        disclosed_data_categories=("thumbnail_prompt",),
        purpose="thumbnail generation",
        expected_retention="provider policy",
        expected_cost=0.04,
        approved_at="2026-07-20T00:00:00+00:00",
        associated_call_ids=("call-1",),
    )
    vault_reference = VaultReferenceRecord(
        record_id="vault-ref-1",
        creator_id="creator-1",
        provider="openai",
        store_kind="macos_keychain",
        opaque_reference="keychain://ReelBrain/openai",
        permitted_tool_ids=("openai-gpt-image-2",),
    )
    run_state = RunStateRecord(
        record_id="run-state-1",
        run_id="run-1",
        project_id="project-1",
        epoch=1,
        previous=RunState.DRAFT,
        current=RunState.AUTO_VERIFIED,
        actor_identity_id="showrunner-1",
        reason="objective gates passed",
        evidence=("verification-report-1",),
    )
    tombstone = DeletionTombstoneRecord(
        record_id="tombstone-1",
        creator_id="creator-1",
        subject_type="transcript",
        subject_id="transcript-2",
        subject_digest="sha256:deleted-record",
        scope="project-1",
        deletion_receipt_id="deletion-receipt-1",
        fence_propagation_state="verified",
    )
    agent_identity = AgentIdentityRecord(
        record_id="agent-identity-1",
        agent_id="showrunner-1",
        role="Showrunner",
        version="1.0.0",
        allowed_acp_surfaces=("tool.search", "tool.request"),
        capability_subject="agent:showrunner-1",
        configuration_digest="sha256:bundle-1",
    )

    ontology = ReelBrainOntologyRecords(
        transcripts=(transcript,),
        captions=(caption,),
        thumbnails=(thumbnail,),
        provider_consents=(provider_consent,),
        vault_references=(vault_reference,),
        run_states=(run_state,),
        deletion_tombstones=(tombstone,),
        agent_identities=(agent_identity,),
    )
    document = ontology.to_document()

    assert set(document) == {
        "transcripts",
        "captions",
        "thumbnails",
        "provider_consents",
        "vault_references",
        "run_states",
        "deletion_tombstones",
        "agent_identities",
    }
    assert set(document["vault_references"][0]) == {
        "record_id",
        "creator_id",
        "provider",
        "store_kind",
        "opaque_reference",
        "permitted_tool_ids",
        "expires_at",
        "revoked",
    }
    assert "text" not in {item.name for item in fields(DeletionTombstoneRecord)}


def test_ontology_records_enforce_caption_vault_and_state_boundaries():
    with pytest.raises(ValueError, match="caption_exceeds_two_lines"):
        CaptionRecord(
            record_id="caption-1",
            creator_id="creator-1",
            project_id="project-1",
            transcript_record_ids=("transcript-1",),
            output_artifact_id="short-1",
            start=0,
            end=1,
            rendered_text="one\ntwo\nthree",
            style_reference="style-1",
        )

    with pytest.raises(ValueError, match="vault_reference_must_be_opaque"):
        VaultReferenceRecord(
            record_id="vault-ref-1",
            creator_id="creator-1",
            provider="openai",
            store_kind="macos_keychain",
            opaque_reference="sk-raw-secret",
            permitted_tool_ids=("openai-gpt-image-2",),
        )

    with pytest.raises(ValueError, match="run_state_transition_required"):
        RunStateRecord(
            record_id="run-state-1",
            run_id="run-1",
            project_id="project-1",
            epoch=1,
            previous=RunState.DRAFT,
            current=RunState.DRAFT,
            actor_identity_id="showrunner-1",
            reason="no transition",
        )
