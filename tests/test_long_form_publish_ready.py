import json

import pytest

from reelbrain.editing import LocalPackageBuilder, RightsEntry, TranscriptSegment, probe_media
from tests.media_fixtures import synthetic_video


def rights():
    return (
        RightsEntry(
            asset_id="source-video",
            source="creator-owned",
            status="approved",
            license_id="creator-license",
            permitted_uses=("long_form_export",),
        ),
    )


def argument_segment(index, start, duration):
    return TranscriptSegment(
        segment_id=f"argument-{index}",
        start=start,
        end=start + duration,
        text=f"Argument {index} explains the ReelBrain architecture and its constraints.",
        thesis=f"Chapter {index}: architecture",
        takeaway=f"argument-takeaway-{index}",
        hook=f"Why chapter {index} matters",
        payoff=f"Chapter {index} resolved",
        required_context=() if index == 1 else (f"argument-{index-1}",),
        confidence=0.99,
        complete_thought=True,
        must_keep=True,
    )


@pytest.fixture(scope="module")
def source_video(tmp_path_factory):
    return synthetic_video(tmp_path_factory.mktemp("long-source") / "source.mp4", duration=1201)


def test_builds_real_5_minute_16x9_publish_ready_long_package(source_video, tmp_path):
    segments = (
        argument_segment(1, 10, 100),
        argument_segment(2, 120, 100),
        argument_segment(3, 230, 100),
    )
    package = LocalPackageBuilder(output_fps=1).build_long_package(
        source=source_video,
        argument_map=segments,
        output_dir=tmp_path / "package",
        project_id="project-1",
        creator_id="creator-1",
        rights=rights(),
        corrected_transcript="\n".join(segment.text for segment in segments),
        creator_approval_receipt="creator-approved-long-1",
        cost_receipt={"currency": "USD", "reserved": 0, "actual": 0},
    )

    info = probe_media(package.videos[0])
    assert info.video_stream.codec_name == "h264"
    assert info.audio_stream.codec_name == "aac"
    assert (info.video_stream.width, info.video_stream.height) == (1920, 1080)
    assert 300 <= info.duration_seconds <= 720
    for key in (
        "chapters",
        "thumbnail",
        "render_recipe",
        "argument_map",
        "corrected_transcript",
        "provenance",
        "cost_receipt",
        "approval_history",
    ):
        assert package.extras[key].is_file()
    audit = json.loads(package.audit_report.read_text())
    assert audit["status"] == "PUBLISH_READY"
    assert audit["argument_map_preserved"] is True
    assert audit["caption_validation"]["caption_word_error_rate"] == 0
    assert audit["caption_validation"]["timing_usable"] is True
    assert audit["caption_validation"]["layout_passed"] is True


def test_long_package_requires_creator_approval(source_video, tmp_path):
    with pytest.raises(ValueError, match="creator_approval_receipt_required"):
        LocalPackageBuilder(output_fps=1).build_long_package(
            source=source_video,
            argument_map=(argument_segment(1, 0, 300),),
            output_dir=tmp_path / "unapproved",
            project_id="project-1",
            creator_id="creator-1",
            rights=rights(),
            corrected_transcript="text",
            creator_approval_receipt="",
            cost_receipt={"actual": 0},
        )


def test_long_package_requires_five_to_twelve_minutes(source_video, tmp_path):
    with pytest.raises(Exception, match="long_form_duration_must_be_5_to_12_minutes"):
        LocalPackageBuilder(output_fps=1).build_long_package(
            source=source_video,
            argument_map=(argument_segment(1, 0, 299),),
            output_dir=tmp_path / "too-short",
            project_id="project-1",
            creator_id="creator-1",
            rights=rights(),
            corrected_transcript="text",
            creator_approval_receipt="approved",
            cost_receipt={"actual": 0},
        )


def test_argument_map_rejects_incomplete_thoughts(source_video, tmp_path):
    incomplete = argument_segment(1, 0, 300)
    incomplete = TranscriptSegment(**{**incomplete.__dict__, "complete_thought": False})

    with pytest.raises(Exception, match="argument_map_incomplete"):
        LocalPackageBuilder(output_fps=1).build_long_package(
            source=source_video,
            argument_map=(incomplete,),
            output_dir=tmp_path / "incomplete",
            project_id="project-1",
            creator_id="creator-1",
            rights=rights(),
            corrected_transcript="text",
            creator_approval_receipt="approved",
            cost_receipt={"actual": 0},
        )


def test_long_package_rejects_caption_text_not_grounded_in_corrected_transcript(
    source_video, tmp_path
):
    segment = argument_segment(1, 0, 300)

    with pytest.raises(Exception, match="long_caption_validation_failed"):
        LocalPackageBuilder(output_fps=1).build_long_package(
            source=source_video,
            argument_map=(segment,),
            output_dir=tmp_path / "caption-mismatch",
            project_id="project-1",
            creator_id="creator-1",
            rights=rights(),
            corrected_transcript="This transcript says something materially different.",
            creator_approval_receipt="approved",
            cost_receipt={"actual": 0},
        )
