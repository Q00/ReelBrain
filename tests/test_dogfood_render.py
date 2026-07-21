from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from PIL import Image

from reelbrain.dogfood_render import (
    BilingualCaptionCue,
    DogfoodRenderError,
    DogfoodRenderer,
    RenderSegment,
    _escape_ass_text,
)
from tests.media_fixtures import require_ffmpeg, synthetic_video


def _pattern_video(path: Path, *, duration: int) -> Path:
    require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _image(path: Path) -> Path:
    require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "gradients=size=640x360:c0=0x102A43:c1=0xD64545:x0=0:y0=0:x1=640:y1=360",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _raw_frame(path: Path, *, at: float, video_filter: str) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(at),
            "-i",
            str(path),
            "-vf",
            f"{video_filter},format=rgb24",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _captions(duration: float) -> tuple[BilingualCaptionCue, ...]:
    return (
        BilingualCaptionCue(0, min(4, duration), "기억은 증거가 아니다", "Memory is not evidence"),
        BilingualCaptionCue(
            min(5, duration - 2),
            min(9, duration),
            "기억은 행동의 사전확률이다",
            "Memory is a behavioral prior",
        ),
    )


@pytest.fixture(scope="module")
def short_source(tmp_path_factory) -> Path:
    return _pattern_video(tmp_path_factory.mktemp("dogfood-short-source") / "source.mp4", duration=40)


@pytest.fixture(scope="module")
def long_source(tmp_path_factory) -> Path:
    return synthetic_video(
        tmp_path_factory.mktemp("dogfood-long-source") / "source.mp4", duration=602
    )


def _renderer(output_root: Path, source_root: Path) -> DogfoodRenderer:
    return DogfoodRenderer(
        output_root,
        project_id="founder-dogfood",
        creator_id="founder",
        read_roots=(source_root,),
        output_fps=1,
        ffmpeg_preset="ultrafast",
    )


def test_renders_centered_blurred_bilingual_short_with_exact_text(short_source, tmp_path):
    output_root = tmp_path / "render results"
    title = "기억 {prior}: 증거가 아니다"
    cues = _captions(32)
    artifacts = _renderer(output_root, short_source.parent).render_short(
        source=short_source,
        start=2,
        duration=32,
        title=title,
        captions=cues,
        output="short.mp4",
    )

    media = _probe(artifacts.video)
    streams = media["streams"]
    assert sorted(stream["codec_type"] for stream in streams) == ["audio", "video"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"]) == (
        "h264",
        1080,
        1920,
    )
    assert audio["codec_name"] == "aac"
    assert 31.5 <= float(media["format"]["duration"]) <= 32.5

    assert "기억은 증거가 아니다" in artifacts.korean_srt.read_text(encoding="utf-8")
    assert "Memory is not evidence" in artifacts.english_srt.read_text(encoding="utf-8")
    ass = artifacts.combined_ass.read_text(encoding="utf-8")
    assert _escape_ass_text(title, allow_newlines=True) in ass
    assert "기억은 증거가 아니다\\NMemory is not evidence" in ass
    assert ass.count("\\N") == len(cues)

    # At t=1 in the Short, the center is the full t=3 source frame scaled to fit.
    # A crop/fill implementation would differ substantially from this reference.
    source_frame = _raw_frame(
        short_source, at=3, video_filter="scale=1080:608"
    )
    centered_frame = _raw_frame(
        artifacts.video, at=1, video_filter="crop=1080:608:0:656"
    )
    assert len(source_frame) == len(centered_frame)
    mean_absolute_error = sum(
        abs(source_byte - output_byte)
        for source_byte, output_byte in zip(source_frame, centered_frame)
    ) / len(source_frame)
    assert mean_absolute_error < 12


def test_caption_contract_rejects_more_than_two_burned_lines(short_source, tmp_path):
    renderer = _renderer(tmp_path / "outputs", short_source.parent)
    with pytest.raises(DogfoodRenderError, match="combined_caption_must_be_two_lines"):
        renderer.render_short(
            source=short_source,
            start=0,
            duration=30,
            title="두 줄 자막",
            captions=(BilingualCaptionCue(0, 3, "첫 줄\n둘째 줄", "A third line"),),
            output="invalid.mp4",
        )


def test_renders_ten_minute_long_form_in_supplied_natural_order(long_source, tmp_path):
    output_root = tmp_path / "long-render"
    segments = (
        RenderSegment(300, 600, label="payoff"),
        RenderSegment(0, 300, label="setup"),
    )
    artifacts = _renderer(output_root, long_source.parent).render_long(
        source=long_source,
        segments=segments,
        captions=(
            BilingualCaptionCue(0, 4, "결론부터 시작합니다", "We begin with the conclusion"),
            BilingualCaptionCue(300, 304, "이제 근거를 설명합니다", "Now we explain the evidence"),
        ),
        output="long.mp4",
        title="ReelBrain의 진화",
    )

    media = _probe(artifacts.video)
    streams = media["streams"]
    assert sorted(stream["codec_type"] for stream in streams) == ["audio", "video"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"]) == (
        "h264",
        1920,
        1080,
    )
    assert audio["codec_name"] == "aac"
    assert 599.5 <= float(media["format"]["duration"]) <= 600.5
    assert "결론부터 시작합니다" in artifacts.korean_srt.read_text(encoding="utf-8")
    assert "We begin with the conclusion" in artifacts.english_srt.read_text(encoding="utf-8")


def test_long_form_requires_natural_segment_boundaries(long_source, tmp_path):
    renderer = _renderer(tmp_path / "outputs", long_source.parent)
    with pytest.raises(DogfoodRenderError, match="long_form_natural_boundaries_required"):
        renderer.render_long(
            source=long_source,
            segments=(RenderSegment(0, 600, natural_end=False),),
            captions=_captions(600),
            output="abrupt.mp4",
        )


@pytest.mark.parametrize(
    ("orientation", "size"),
    (("vertical", (1080, 1920)), ("horizontal", (1920, 1080))),
)
def test_overlays_exact_korean_thumbnail_title_without_provider_calls(
    orientation, size, tmp_path
):
    background = _image(tmp_path / "input images" / "GPT background.png")
    output_root = tmp_path / "thumbnails"
    title = "에이전트의 '꿈': 기억 ≠ 증거"
    artifacts = _renderer(output_root, background.parent).render_thumbnail(
        background=background,
        title=title,
        output=f"{orientation}.png",
        orientation=orientation,
    )

    media = _probe(artifacts.image)
    assert [stream["codec_type"] for stream in media["streams"]] == ["video"]
    assert (media["streams"][0]["width"], media["streams"][0]["height"]) == size
    ass = artifacts.overlay_ass.read_text(encoding="utf-8")
    assert _escape_ass_text(title, allow_newlines=True) in ass
