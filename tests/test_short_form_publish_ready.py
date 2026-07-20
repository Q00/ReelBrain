import json

import pytest

from reelbrain.editing import (
    LocalPackageBuilder,
    MediaError,
    RightsEntry,
    TranscriptSegment,
    probe_media,
    word_error_rate,
)
from tests.media_fixtures import synthetic_video
from reelbrain.transcription import TranscriptChunk


def segment(index, start, *, takeaway=None, self_contained=True, complete=True):
    return TranscriptSegment(
        segment_id=f"segment-{index}",
        start=start,
        end=start + 35,
        text=f"Lesson {index}: memory is a behavioral prior, not evidence.",
        thesis=f"Lesson {index}",
        takeaway=takeaway or f"takeaway-{index}",
        hook=f"The surprising part of lesson {index}",
        payoff=f"You can now apply lesson {index}",
        confidence=0.95,
        educational_value=1 - index * 0.01,
        self_contained=self_contained,
        complete_thought=complete,
    )


def rights():
    return (
        RightsEntry(
            asset_id="source-video",
            source="creator-owned",
            status="approved",
            license_id="creator-license",
            permitted_uses=("short_form_export", "long_form_export"),
        ),
    )


@pytest.fixture(scope="module")
def source_video(tmp_path_factory):
    return synthetic_video(tmp_path_factory.mktemp("short-source") / "source.mp4", duration=301)


def test_builds_three_real_diverse_9x16_h264_aac_candidates(source_video, tmp_path):
    package = LocalPackageBuilder(output_fps=2).build_short_package(
        source=source_video,
        segments=(segment(1, 10), segment(2, 60), segment(3, 110), segment(4, 160)),
        output_dir=tmp_path / "package",
        project_id="project-1",
        creator_id="creator-1",
        rights=rights(),
        approved_thumbnail=True,
    )

    assert len(package.videos) == 4  # 3 candidates plus selected final
    for video in package.videos:
        info = probe_media(video)
        assert info.video_stream.codec_name == "h264"
        assert (info.video_stream.width, info.video_stream.height) == (1080, 1920)
        assert info.audio_stream.codec_name == "aac"
        assert 30 <= info.duration_seconds <= 60
    for artifact in (
        package.captions_srt,
        package.captions_vtt,
        package.otio_timeline,
        package.asset_manifest,
        package.rights_manifest,
        package.traceability_map,
        package.audit_report,
        package.extras["educational_value_cards"],
        package.extras["thumbnail"],
        package.extras["metadata_draft"],
    ):
        assert artifact.is_file() and artifact.stat().st_size > 0
    audit = json.loads(package.audit_report.read_text())
    assert audit["candidate_count"] == 3
    assert audit["diverse_takeaways"] is True
    for name in (
        "governance_acp_registry",
        "governance_capability_receipts",
        "governance_toolbox_manifests",
        "governance_provider_receipts",
        "governance_budget_ledger",
        "governance_rights_manifest",
        "governance_denial_logs",
        "governance_approval_records",
    ):
        assert package.extras[name].is_file()


def test_selection_rejects_mid_thought_or_non_self_contained_candidates():
    builder = LocalPackageBuilder()

    with pytest.raises(MediaError, match="insufficient_diverse_short_candidates"):
        builder.select_short_candidates(
            (
                segment(1, 0, self_contained=False),
                segment(2, 40, complete=False),
                segment(3, 80),
            )
        )


def test_diversity_rejects_duplicate_takeaway_even_with_high_scores():
    builder = LocalPackageBuilder()

    selected = builder.select_short_candidates(
        (
            segment(1, 0, takeaway="same"),
            segment(2, 50, takeaway="same"),
            segment(3, 100, takeaway="different-1"),
            segment(4, 150, takeaway="different-2"),
        )
    )

    assert {item.takeaway for item in selected} == {"same", "different-1", "different-2"}


def test_caption_accuracy_gate_uses_real_wer_threshold():
    reference = "memory is a behavioral prior not evidence"
    hypothesis = "memory is a behavioral prior not evidence"
    degraded = "memory is evidence"

    assert word_error_rate(reference, hypothesis) == 0
    assert word_error_rate(reference, degraded) > 0.05


def test_rights_are_non_waivable_for_short_export(source_video, tmp_path):
    denied = RightsEntry(
        asset_id="source-video",
        source="unknown",
        status="denied",
        license_id="none",
        permitted_uses=(),
    )

    with pytest.raises(PermissionError, match="rights_do_not_permit_export"):
        LocalPackageBuilder().build_short_package(
            source=source_video,
            segments=(segment(1, 10), segment(2, 60), segment(3, 110)),
            output_dir=tmp_path / "denied",
            project_id="project-1",
            creator_id="creator-1",
            rights=(denied,),
        )


def test_creator_can_supply_only_video_and_runtime_discovers_publish_ready_highlights(
    source_video, tmp_path
):
    class FixtureSTT:
        name = "fixture-stt"

        def transcribe(self, video_path):
            assert video_path == source_video.resolve()
            return (
                TranscriptChunk(
                    "auto-1",
                    10,
                    45,
                    "Memory is a behavioral prior, not evidence. This distinction prevents false certainty.",
                ),
                TranscriptChunk(
                    "auto-2",
                    60,
                    95,
                    "ACP translates tool capabilities for agents. The broker still enforces every side effect.",
                ),
                TranscriptChunk(
                    "auto-3",
                    110,
                    145,
                    "Sleep improves bounded configurations offline. Hidden evaluation and rollback keep it safe.",
                ),
            )

    package = LocalPackageBuilder(output_fps=2).build_short_from_video(
        source=source_video,
        stt_provider=FixtureSTT(),
        output_dir=tmp_path / "automatic-package",
        project_id="project-1",
        creator_id="creator-1",
        rights=rights(),
        creator_approval_receipt="creator-approved-short-1",
        preferred_terms=("memory", "agents", "sleep"),
    )

    audit = json.loads(package.audit_report.read_text())
    assert audit["status"] == "PUBLISH_READY"
    assert audit["highlight_discovery"] == "agent_fan_out"
    assert audit["source_faithful"] is True
    assert audit["meaning_changing_caption_errors"] == 0
    assert package.extras["source_transcript"].is_file()
    assert package.extras["agent_assessments"].is_file()
