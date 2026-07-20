import json

from reelbrain.planning import LongFormPlanBuilder
from reelbrain.transcription import SubtitleFileSTT
from tests.media_fixtures import synthetic_video


def test_long_plan_is_agent_ranked_but_requires_creator_confirmation(tmp_path):
    source = synthetic_video(tmp_path / "source.mp4", duration=1201)
    transcript = tmp_path / "source.srt"
    transcript.write_text(
        "\n".join(
            f"""{index + 1}
00:{index:02}:00,000 --> 00:{index + 1:02}:00,000
Chapter {index + 1} explains one complete educational idea about governed agents.
"""
            for index in range(7)
        ),
        encoding="utf-8",
    )

    artifacts = LongFormPlanBuilder().propose(
        source=source,
        transcript_provider=SubtitleFileSTT(transcript),
        output_dir=tmp_path / "plan",
        project_id="project-long-plan",
        creator_id="creator-1",
        preferred_terms=("agents",),
    )

    argument_map = json.loads(artifacts["argument_map"].read_text())
    report = json.loads(artifacts["report"].read_text())
    assessments = json.loads(artifacts["agent_assessments"].read_text())

    assert 300 <= report["selected_duration_seconds"] <= 720
    assert report["status"] == "CREATOR_CONFIRMATION_REQUIRED"
    assert report["creator_confirmed"] is False
    assert report["publish_ready"] is False
    assert [row["start"] for row in argument_map] == sorted(
        row["start"] for row in argument_map
    )
    assert len(assessments) >= len(argument_map) * 4
    assert argument_map[0]["required_context"] == []
    assert argument_map[1]["required_context"] == [argument_map[0]["segment_id"]]
