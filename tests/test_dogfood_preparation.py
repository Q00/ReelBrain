from pathlib import Path
from hashlib import sha256
import json
from zipfile import ZipFile

import pytest

from reelbrain.dogfood import (
    DogfoodRunConfig,
    FounderDogfoodRunner,
    bilingual_caption_cues_for_range,
    discover_video_sources,
    load_bilingual_transcript,
    prepare_dogfood_run,
    write_bilingual_transcript,
)
from reelbrain.dogfood_render import ThumbnailArtifacts, VideoRenderArtifacts
from reelbrain.editorial import EditorialPlan, LongFormDraftPlan, ShortDraft
from reelbrain.provider_plan import ProviderAuthorizationPlan
from reelbrain.editing import MediaError
from reelbrain.transcription import BilingualTranscript, SpeechWindow, TranscriptChunk
from tests.media_fixtures import synthetic_video


def test_dogfood_config_enforces_requested_output_shape(tmp_path):
    DogfoodRunConfig(
        project_id="founder-run",
        creator_id="founder",
        output_root=tmp_path,
        shorts_per_source=2,
        minimum_long_seconds=600,
        maximum_long_seconds=900,
    )
    with pytest.raises(ValueError, match="2_to_10"):
        DogfoodRunConfig(
            project_id="founder-run",
            creator_id="founder",
            output_root=tmp_path,
            shorts_per_source=1,
        )
    with pytest.raises(ValueError, match="must_be_canonical"):
        DogfoodRunConfig(
            project_id="founder-run ",
            creator_id="founder",
            output_root=tmp_path,
        )


def test_archive_extraction_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.mp4", b"not video")

    with pytest.raises(MediaError, match="unsafe_zip_member_path"):
        discover_video_sources(archive, tmp_path / "input")


def test_archive_extraction_rejects_duplicate_casefolded_targets(tmp_path):
    archive = tmp_path / "duplicates.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("Video.mp4", b"one")
        bundle.writestr("video.mp4", b"two")

    with pytest.raises(MediaError, match="duplicate_target"):
        discover_video_sources(archive, tmp_path / "input")


def test_prepare_dogfood_run_writes_inventory_plan_and_review_manifest(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    synthetic_video(source_dir / "교육 세미나.mp4", duration=5)
    output = tmp_path / "run"
    artifacts = prepare_dogfood_run(
        input_path=source_dir,
        config=DogfoodRunConfig(
            project_id="founder-run",
            creator_id="founder",
            output_root=output,
            shorts_per_source=3,
        ),
    )

    assert set(artifacts) == {"source_inventory", "provider_plan", "run_manifest"}
    assert all(path.is_file() for path in artifacts.values())
    assert "AWAITING_PROVIDER_APPROVAL" in artifacts["run_manifest"].read_text()
    assert "durable_preference_change\": false" in artifacts["run_manifest"].read_text()


def test_source_discovery_deduplicates_alias_symlinks(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    original = synthetic_video(source_dir / "original.mp4", duration=2)
    (source_dir / "source_01.mp4").symlink_to(original)

    assert discover_video_sources(source_dir, tmp_path / "unused") == (original.resolve(),)


def test_bilingual_caption_alignment_is_two_lines_readable_and_round_trips(tmp_path):
    transcript = BilingualTranscript(
        korean=(
            TranscriptChunk("ko-1", 10, 14, "메모리는 증거가 아니라 행동의 사전 분포입니다."),
            TranscriptChunk("ko-2", 14, 18, "그래서 현재의 관찰이 항상 우선합니다."),
        ),
        english=(
            TranscriptChunk(
                "en-1", 10, 14, "Memory is a behavioral prior, not evidence."
            ),
            TranscriptChunk(
                "en-2", 14, 18, "That is why current observations always win."
            ),
        ),
        speech_windows=(SpeechWindow(10, 18),),
    )
    loaded = load_bilingual_transcript(
        write_bilingual_transcript(transcript, tmp_path / "bilingual.json")
    )
    cues = bilingual_caption_cues_for_range(
        loaded, source_start=10, source_end=18
    )

    assert cues[0].start == 0
    assert cues[-1].end == 8
    assert all(len(cue.korean) <= 42 for cue in cues)
    assert all(len(cue.english) <= 64 for cue in cues)
    assert all("\n" not in cue.korean + cue.english for cue in cues)


def test_bilingual_caption_segmentation_converges_for_large_canonical_cue():
    korean_text = " ".join(f"긴한국어설명{index}" for index in range(220))
    english_text = " ".join(
        f"long English educational explanation {index}" for index in range(260)
    )
    transcript = BilingualTranscript(
        korean=(TranscriptChunk("ko-large", 0, 120, korean_text),),
        english=(TranscriptChunk("en-large", 0, 120, english_text),),
        speech_windows=(SpeechWindow(0, 120),),
    )

    cues = bilingual_caption_cues_for_range(
        transcript, source_start=0, source_end=120
    )

    assert cues[0].start == 0
    assert cues[-1].end == 120
    assert all(len(cue.korean) <= 42 for cue in cues)
    assert all(len(cue.english) <= 64 for cue in cues)


def test_founder_runner_stays_creator_review_and_writes_evolution_evidence(tmp_path):
    source = synthetic_video(tmp_path / "source.mp4", duration=601)
    transcript = BilingualTranscript(
        korean=tuple(
            TranscriptChunk(f"ko-{index}", index * 10, (index + 1) * 10, f"한국어 설명 {index}.")
            for index in range(60)
        ),
        english=tuple(
            TranscriptChunk(f"en-{index}", index * 10, (index + 1) * 10, f"English lesson {index}.")
            for index in range(60)
        ),
        speech_windows=(SpeechWindow(0, 600),),
    )
    editorial = EditorialPlan(
        shorts=tuple(
            ShortDraft(
                candidate_id=f"short-{index}",
                chunk_ids=(f"ko-{index * 4}",),
                start_seconds=start,
                end_seconds=start + 30,
                text=f"Short lesson {index}",
                angle=f"Hook {index}",
                rationale="grounded fixture",
            )
            for index, start in enumerate((10, 60, 110), start=1)
        ),
        long_form=LongFormDraftPlan(
            window_id="long-1",
            chunk_ids=tuple(f"ko-{index}" for index in range(60)),
            start_seconds=0,
            end_seconds=600,
            title="Long lesson",
            thesis="A coherent lesson",
            rationale="natural complete window",
            sections=(),
        ),
        persona_selections=(),
        trace=(),
    )

    class FakeTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe_bilingual(self, *args, **kwargs):
            self.calls += 1
            return transcript

    class FakeEditorialTeam:
        def __init__(self):
            self.calls = 0

        def plan(self, *args, **kwargs):
            self.calls += 1
            return editorial

    class FakeImageTool:
        def __init__(self):
            self.calls = 0

        def generate_thumbnail(
            self, *, output_path, prompt, provider_consent_receipt, **kwargs
        ):
            self.calls += 1
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fixture-image")
            Path(str(output_path) + ".provenance.json").write_text(
                json.dumps(
                    {
                        "model": "gpt-image-2",
                        "provider_invocation_id": provider_consent_receipt["invocation_id"],
                        "prompt_sha256": sha256(prompt.encode()).hexdigest(),
                        "image_sha256": sha256(b"fixture-image").hexdigest(),
                    }
                )
            )

    class FakeGuard:
        def write_audit_artifacts(self, output_dir, *, rights_manifest):
            root = Path(output_dir)
            root.mkdir(parents=True, exist_ok=True)
            path = root / "audit.json"
            path.write_text("{}")
            return {"audit": path}

    class FakeRenderer:
        def __init__(self, output_root, **kwargs):
            self.output_root = Path(output_root)
            self.guard = FakeGuard()

        def _video(self, output, duration):
            video = Path(output)
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b"video")
            ko = video.with_suffix(".ko.srt")
            en = video.with_suffix(".en.srt")
            ass = video.with_suffix(".ass")
            for path in (ko, en, ass):
                path.write_text("fixture")
            return VideoRenderArtifacts(video, ko, en, ass, duration)

        def render_short(self, *, output, duration, **kwargs):
            return self._video(output, duration)

        def render_long(self, *, output, segments, **kwargs):
            return self._video(output, sum(segment.duration for segment in segments))

        def render_thumbnail(self, *, output, orientation, **kwargs):
            image = Path(output)
            image.write_bytes(b"thumbnail")
            overlay = image.with_suffix(".ass")
            overlay.write_text("fixture")
            return ThumbnailArtifacts(image, overlay, orientation)

    root = tmp_path / "run"
    provider_plan = ProviderAuthorizationPlan.founder_dogfood(
        project_id="founder-run",
        creator_id="founder",
        source_count=1,
        shorts_per_source=3,
        source_asset_digests=(sha256(source.read_bytes()).hexdigest(),),
        approved=True,
        approval_receipt_id="creator-approved-provider-cap",
    ).write(root / "provider_plan.json")
    env = tmp_path / ".env"
    env.write_text("OPEN_API_KEY=fixture-never-resolved")
    fake_transcriber = FakeTranscriber()
    fake_editorial = FakeEditorialTeam()
    fake_image = FakeImageTool()
    runner = FounderDogfoodRunner(
        transcriber=fake_transcriber,
        editorial_team=fake_editorial,
        image_tool=fake_image,
        renderer_factory=FakeRenderer,
        allow_test_adapters=True,
    )
    run_kwargs = dict(
        sources=(source,),
        config=DogfoodRunConfig(
            project_id="founder-run",
            creator_id="founder",
            output_root=root,
            shorts_per_source=3,
        ),
        provider_plan_path=provider_plan,
        env_file=env,
        image_approval_receipt="creator-requested-gpt-image-2",
    )
    artifacts = runner.run(**run_kwargs)
    runner.run(**run_kwargs)

    manifest = artifacts["manifest"].read_text()
    evolution = artifacts["evolution_report"].read_text()
    assert '"status": "CREATOR_REVIEW"' in manifest
    assert '"output_count": 4' in manifest
    assert '"durable_preference_written": false' in evolution
    assert "four independent editorial persona lanes" in evolution
    assert fake_transcriber.calls == 1
    assert fake_editorial.calls == 1
    assert fake_image.calls == 4
