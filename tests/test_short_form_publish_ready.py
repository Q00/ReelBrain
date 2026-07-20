import json

import pytest

from reelbrain.editing import (
    LocalPackageBuilder,
    MediaError,
    RightsEntry,
    TranscriptSegment,
    caption_cues,
    probe_media,
    validate_captions,
    word_error_rate,
)
from tests.media_fixtures import synthetic_video
from reelbrain.transcription import SubtitleFileSTT, TranscriptChunk


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


def test_caption_validation_uses_independent_reference_and_readable_timing():
    reference = "memory is a behavioral prior not evidence and it changes agent behavior"
    cues = caption_cues(reference, 35)
    validation = validate_captions(
        source_reference=reference,
        highlight_text=reference,
        cues=cues,
        reference_kind="gold_fixture",
        reference_confidence=1.0,
    )

    assert validation.passed is True
    assert validation.caption_word_error_rate == 0
    assert validation.meaning_changing_caption_errors == 0
    assert all(cue.end - cue.start <= 6.01 for cue in cues)
    assert all(len(cue.text.splitlines()) <= 2 for cue in cues)


def test_caption_validation_rejects_self_attestation_and_word_changes():
    reference = "memory is not evidence"
    changed = "memory is evidence"
    cues = caption_cues(changed, 5)
    validation = validate_captions(
        source_reference=reference,
        highlight_text=changed,
        cues=cues,
        reference_kind="self_attested",
        reference_confidence=1.0,
    )

    assert validation.passed is False
    assert validation.meaning_changing_caption_errors > 0


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
        official = True
        provider = None
        reference_kind = "gold_fixture"

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
    assert audit["caption_validation"]["caption_word_error_rate"] == 0
    assert audit["caption_validation"]["timing_usable"] is True
    assert audit["caption_validation"]["layout_passed"] is True
    assert package.extras["source_transcript"].is_file()
    assert package.extras["agent_assessments"].is_file()


def test_normal_short_whisper_chunks_are_combined_into_30_to_60_second_windows():
    chunks = tuple(
        TranscriptChunk(
            f"chunk-{index}",
            index * 5,
            (index + 1) * 5,
            f"Part {index} of one educational explanation{' .' if index % 7 == 6 else ''}",
            0.95,
        )
        for index in range(21)
    )

    segments = LocalPackageBuilder.segments_from_transcript_chunks(chunks)

    assert len(segments) == 3
    assert all(30 <= segment.duration <= 60 for segment in segments)
    assert all(segment.complete_thought for segment in segments)


def test_creator_supplied_srt_can_replace_missing_whisper_for_short_ingest(
    source_video, tmp_path
):
    transcript_dir = tmp_path / "creator-transcript"
    transcript_dir.mkdir()
    transcript = transcript_dir / "source.srt"
    transcript.write_text(
        """1
00:00:10,000 --> 00:00:45,000
Memory is a behavioral prior, not evidence.

2
00:01:00,000 --> 00:01:35,000
ACP translates tool capabilities while the broker enforces every effect.

3
00:01:50,000 --> 00:02:25,000
Sleep promotes only bounded configurations after hidden evaluation and rollback.
""",
        encoding="utf-8",
    )

    package = LocalPackageBuilder(output_fps=2).build_short_from_video(
        source=source_video,
        stt_provider=SubtitleFileSTT(transcript),
        output_dir=tmp_path / "subtitle-package",
        project_id="project-subtitle",
        creator_id="creator-1",
        rights=rights(),
        creator_approval_receipt="creator-approved-subtitle-short",
    )

    audit = json.loads(package.audit_report.read_text())
    assert audit["status"] == "PUBLISH_READY"
    assert audit["stt_provider"] == "subtitle-file-stt"
    assert audit["caption_validation"]["reference_kind"] == "creator_supplied_transcript"


def test_short_draft_without_creator_approval_stays_in_creator_review(
    source_video, tmp_path
):
    class FixtureSTT:
        name = "fixture-stt-draft"
        official = True
        provider = None
        reference_kind = "gold_fixture"

        def transcribe(self, video_path):
            return tuple(
                TranscriptChunk(
                    f"draft-{index}",
                    start,
                    start + 35,
                    f"Draft lesson {index} is a complete educational explanation.",
                )
                for index, start in enumerate((10, 60, 110), start=1)
            )

    package = LocalPackageBuilder(output_fps=2).build_short_from_video(
        source=source_video,
        stt_provider=FixtureSTT(),
        output_dir=tmp_path / "draft-package",
        project_id="project-draft",
        creator_id="creator-1",
        rights=rights(),
        creator_approval_receipt="",
    )

    assert json.loads(package.audit_report.read_text())["status"] == "CREATOR_REVIEW"
