"""Governed FFmpeg primitives used by the founder dogfood workflow.

The higher-level dogfood orchestration owns editorial decisions.  This module
only accepts an already selected timeline and exact creator-facing text, then
turns them into deterministic, reviewable media artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Literal

from .runtime_guard import RuntimeGuard


class DogfoodRenderError(RuntimeError):
    """Raised when a render request or its resulting media is invalid."""


@dataclass(frozen=True)
class BilingualCaptionCue:
    """One two-line burned caption: Korean first, English second."""

    start: float
    end: float
    korean: str
    english: str


@dataclass(frozen=True)
class RenderSegment:
    """A creator/agent-selected long-form source range in final story order."""

    start: float
    end: float
    label: str = ""
    natural_start: bool = True
    natural_end: bool = True

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class VideoRenderArtifacts:
    video: Path
    korean_srt: Path
    english_srt: Path
    combined_ass: Path
    duration_seconds: float


@dataclass(frozen=True)
class ThumbnailArtifacts:
    image: Path
    overlay_ass: Path
    orientation: Literal["vertical", "horizontal"]


@dataclass(frozen=True)
class _MediaProbe:
    duration: float | None
    streams: tuple[dict[str, object], ...]


class DogfoodRenderer:
    """Render founder-dogfood videos through ReelBrain's RuntimeGuard.

    Production dimensions remain fixed.  ``output_fps`` and the encoder preset
    are configurable so fixture tests can render the same topology quickly.
    """

    SHORT_SIZE = (1080, 1920)
    LONG_SIZE = (1920, 1080)
    THUMBNAIL_SIZES = {
        "vertical": SHORT_SIZE,
        "horizontal": LONG_SIZE,
    }

    def __init__(
        self,
        output_root: Path | str,
        *,
        project_id: str,
        creator_id: str,
        read_roots: Iterable[Path | str] = (),
        guard: RuntimeGuard | None = None,
        output_fps: int = 30,
        ffmpeg_preset: str = "medium",
    ) -> None:
        if output_fps <= 0:
            raise ValueError("output_fps_must_be_positive")
        self.output_root = Path(output_root).expanduser().resolve(strict=False)
        self.output_fps = output_fps
        self.ffmpeg_preset = ffmpeg_preset
        self.guard = guard or RuntimeGuard(
            workspace_root=self.output_root,
            local_allowlist=(
                *(Path(root).expanduser().resolve(strict=False) for root in read_roots),
                Path("/System/Library/Fonts"),
                Path("/System/Library/Fonts/Supplemental"),
                Path("/Library/Fonts"),
                Path("/usr/share/fonts"),
            ),
            project_id=project_id,
            creator_id=creator_id,
            agent_id="dogfood-renderer",
            tool_names=("ffmpeg", "ffprobe"),
        )
        self.guard.authorize_path(
            self.output_root, operation="write", data_class="dogfood_render_root"
        )
        self.output_root.mkdir(parents=True, exist_ok=True)

    def render_short(
        self,
        *,
        source: Path | str,
        start: float,
        duration: float,
        title: str,
        captions: Iterable[BilingualCaptionCue],
        output: Path | str,
    ) -> VideoRenderArtifacts:
        """Render a captioned 9:16 Short with a centered full 16:9 frame."""

        if not 30 <= duration <= 60:
            raise DogfoodRenderError("short_duration_must_be_30_to_60_seconds")
        if start < 0:
            raise DogfoodRenderError("short_start_must_be_non_negative")
        self._validate_title(title, maximum_lines=2)
        caption_cues = tuple(captions)
        source_path = self._readable_file(source, data_class="source_video")
        source_probe = self._probe(source_path)
        self._require_av_source(source_probe)
        if source_probe.duration is not None and start + duration > source_probe.duration + 0.05:
            raise DogfoodRenderError("short_range_exceeds_source_duration")

        output_path = self._output_path(output, data_class="short_video")
        caption_artifacts = self._write_subtitle_artifacts(
            captions=caption_cues,
            duration=duration,
            output=output_path,
            canvas_size=self.SHORT_SIZE,
            title=title,
            title_end=duration,
        )
        overlay_timeline = self._build_overlay_timeline(
            output=output_path,
            cues=caption_cues,
            duration=duration,
            canvas_size=self.SHORT_SIZE,
            title=title,
            title_end=duration,
        )
        width, height = self.SHORT_SIZE
        # The background consumes the full vertical canvas.  The foreground is
        # independently scaled with `decrease`, so no source pixels are cropped.
        filter_graph = (
            "[0:v]split=2[background_source][foreground_source];"
            f"[background_source]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=luma_radius=24:luma_power=2[background];"
            f"[foreground_source]scale={width}:{height}:force_original_aspect_ratio=decrease"
            "[foreground];"
            "[background][foreground]overlay=(W-w)/2:(H-h)/2,setsar=1[base_video];"
            "[1:v]format=rgba,setpts=PTS-STARTPTS[text_overlay];"
            "[base_video][text_overlay]overlay=0:0:eof_action=repeat:shortest=0[video_out]"
        )
        self._run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                _decimal(start),
                "-i",
                str(source_path),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(overlay_timeline),
                "-t",
                _decimal(duration),
                "-filter_complex",
                filter_graph,
                "-map",
                "[video_out]",
                "-map",
                "0:a:0",
                "-r",
                str(self.output_fps),
                "-c:v",
                "libx264",
                "-preset",
                self.ffmpeg_preset,
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        actual_duration = self._validate_video(
            output_path,
            expected_size=self.SHORT_SIZE,
            minimum_duration=30,
            maximum_duration=60,
        )
        return VideoRenderArtifacts(
            video=output_path,
            korean_srt=caption_artifacts.korean_srt,
            english_srt=caption_artifacts.english_srt,
            combined_ass=caption_artifacts.combined_ass,
            duration_seconds=actual_duration,
        )

    def render_long(
        self,
        *,
        source: Path | str,
        segments: Iterable[RenderSegment],
        captions: Iterable[BilingualCaptionCue],
        output: Path | str,
        title: str | None = None,
    ) -> VideoRenderArtifacts:
        """Render a 10-15 minute 16:9 sequence in the supplied story order."""

        source_path = self._readable_file(source, data_class="source_video")
        source_probe = self._probe(source_path)
        self._require_av_source(source_probe)
        ordered_segments = tuple(segments)
        if not ordered_segments:
            raise DogfoodRenderError("long_form_segments_required")
        if any(segment.start < 0 or segment.end <= segment.start for segment in ordered_segments):
            raise DogfoodRenderError("long_form_segment_range_invalid")
        if any(not segment.natural_start or not segment.natural_end for segment in ordered_segments):
            raise DogfoodRenderError("long_form_natural_boundaries_required")
        if source_probe.duration is not None and any(
            segment.end > source_probe.duration + 0.05 for segment in ordered_segments
        ):
            raise DogfoodRenderError("long_form_segment_exceeds_source_duration")
        total_duration = sum(segment.duration for segment in ordered_segments)
        if not 600 <= total_duration <= 900:
            raise DogfoodRenderError("long_form_duration_must_be_10_to_15_minutes")
        if title is not None:
            self._validate_title(title, maximum_lines=2)
        caption_cues = tuple(captions)

        output_path = self._output_path(output, data_class="long_form_video")
        caption_artifacts = self._write_subtitle_artifacts(
            captions=caption_cues,
            duration=total_duration,
            output=output_path,
            canvas_size=self.LONG_SIZE,
            title=title,
            title_end=min(5.0, total_duration),
        )
        overlay_timeline = self._build_overlay_timeline(
            output=output_path,
            cues=caption_cues,
            duration=total_duration,
            canvas_size=self.LONG_SIZE,
            title=title,
            title_end=min(5.0, total_duration),
        )
        width, height = self.LONG_SIZE
        graph_parts: list[str] = []
        concat_inputs: list[str] = []
        for index, segment in enumerate(ordered_segments):
            graph_parts.extend(
                (
                    f"[0:v]trim=start={_decimal(segment.start)}:end={_decimal(segment.end)},"
                    "setpts=PTS-STARTPTS,"
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[v{index}]",
                    f"[0:a]atrim=start={_decimal(segment.start)}:end={_decimal(segment.end)},"
                    f"asetpts=PTS-STARTPTS[a{index}]",
                )
            )
            concat_inputs.extend((f"[v{index}]", f"[a{index}]"))
        graph_parts.append(
            "".join(concat_inputs)
            + f"concat=n={len(ordered_segments)}:v=1:a=1[video_concat][audio_out]"
        )
        graph_parts.extend(
            (
                "[1:v]format=rgba,setpts=PTS-STARTPTS[text_overlay]",
                "[video_concat][text_overlay]overlay=0:0:"
                "eof_action=repeat:shortest=0[video_out]",
            )
        )
        self._run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(overlay_timeline),
                "-filter_complex",
                ";".join(graph_parts),
                "-t",
                _decimal(total_duration),
                "-map",
                "[video_out]",
                "-map",
                "[audio_out]",
                "-r",
                str(self.output_fps),
                "-c:v",
                "libx264",
                "-preset",
                self.ffmpeg_preset,
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        actual_duration = self._validate_video(
            output_path,
            expected_size=self.LONG_SIZE,
            minimum_duration=600,
            maximum_duration=900,
        )
        return VideoRenderArtifacts(
            video=output_path,
            korean_srt=caption_artifacts.korean_srt,
            english_srt=caption_artifacts.english_srt,
            combined_ass=caption_artifacts.combined_ass,
            duration_seconds=actual_duration,
        )

    def render_thumbnail(
        self,
        *,
        background: Path | str,
        title: str,
        output: Path | str,
        orientation: Literal["vertical", "horizontal"],
    ) -> ThumbnailArtifacts:
        """Overlay exact title text on a caller-supplied generated background."""

        if orientation not in self.THUMBNAIL_SIZES:
            raise ValueError("thumbnail_orientation_invalid")
        self._validate_title(title, maximum_lines=3)
        background_path = self._readable_file(
            background, data_class="generated_thumbnail_background"
        )
        background_probe = self._probe(background_path)
        if not any(stream.get("codec_type") == "video" for stream in background_probe.streams):
            raise DogfoodRenderError("thumbnail_background_video_stream_required")
        output_path = self._output_path(output, data_class="thumbnail")
        width, height = self.THUMBNAIL_SIZES[orientation]
        ass_output = output_path.with_name(f"{output_path.stem}.overlay.ass")
        self._write_ass(
            ass_output,
            cues=(),
            duration=1.0,
            canvas_size=(width, height),
            title=title,
            title_end=1.0,
            thumbnail=True,
        )
        Image, ImageDraw, ImageFont, ImageOps = _pillow_modules()
        try:
            with Image.open(background_path) as opened:
                background_image = ImageOps.fit(
                    opened.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS
                )
        except (OSError, ValueError) as exc:
            raise DogfoodRenderError("thumbnail_background_decode_failed") from exc
        self._draw_text_overlay(
            background_image,
            title=title,
            korean=None,
            english=None,
            thumbnail=True,
            image_draw=ImageDraw,
            image_font=ImageFont,
        )
        save_options = {"quality": 95, "subsampling": 0} if output_path.suffix.lower() in {
            ".jpg",
            ".jpeg",
        } else {}
        background_image.save(output_path, **save_options)
        probe = self._probe(output_path)
        stream_types = [stream.get("codec_type") for stream in probe.streams]
        video = next(
            (stream for stream in probe.streams if stream.get("codec_type") == "video"),
            None,
        )
        if stream_types != ["video"] or video is None:
            raise DogfoodRenderError("thumbnail_must_have_one_video_stream")
        if (video.get("width"), video.get("height")) != (width, height):
            raise DogfoodRenderError("thumbnail_resolution_mismatch")
        return ThumbnailArtifacts(output_path, ass_output, orientation)

    def _build_overlay_timeline(
        self,
        *,
        output: Path,
        cues: tuple[BilingualCaptionCue, ...],
        duration: float,
        canvas_size: tuple[int, int],
        title: str | None,
        title_end: float,
    ) -> Path:
        """Rasterize exact text states and describe their timing for FFmpeg.

        This fallback is intentionally independent of FFmpeg's optional libass
        and drawtext builds.  The canonical ASS remains a sidecar while Pillow
        rasterizes the exact same strings to transparent PNGs.
        """

        _pillow_modules()  # Fail before partially writing an unusable timeline.
        overlay_root = output.with_suffix("").with_name(f"{output.stem}.overlays")
        self.guard.authorize_path(
            overlay_root, operation="write", data_class="caption_overlay_directory"
        )
        overlay_root.mkdir(parents=True, exist_ok=True)
        boundaries = {0.0, duration}
        if title is not None:
            boundaries.add(max(0.0, min(title_end, duration)))
        for cue in cues:
            boundaries.update((cue.start, cue.end))
        ordered = sorted(boundaries)
        state_paths: dict[tuple[bool, int | None], Path] = {}
        rows: list[dict[str, object]] = []
        concat_lines = ["ffconcat version 1.0"]
        last_path: Path | None = None
        for start, end in zip(ordered, ordered[1:]):
            if end <= start:
                continue
            midpoint = start + (end - start) / 2
            cue_index = next(
                (
                    index
                    for index, cue in enumerate(cues)
                    if cue.start <= midpoint < cue.end
                ),
                None,
            )
            title_active = title is not None and midpoint < title_end
            key = (title_active, cue_index)
            state_path = state_paths.get(key)
            if state_path is None:
                state_path = overlay_root / f"state_{len(state_paths):04}.png"
                cue = cues[cue_index] if cue_index is not None else None
                self._render_text_overlay(
                    state_path,
                    canvas_size=canvas_size,
                    title=title if title_active else None,
                    korean=cue.korean if cue is not None else None,
                    english=cue.english if cue is not None else None,
                )
                state_paths[key] = state_path
            concat_lines.extend(
                (f"file '{state_path.name}'", f"duration {_decimal(end - start)}")
            )
            rows.append(
                {
                    "start": start,
                    "end": end,
                    "title": title if title_active else None,
                    "korean": cues[cue_index].korean if cue_index is not None else None,
                    "english": cues[cue_index].english if cue_index is not None else None,
                    "image": state_path.name,
                }
            )
            last_path = state_path
        if last_path is None:
            raise DogfoodRenderError("caption_overlay_timeline_empty")
        # The concat demuxer needs a repeated terminal file to honor the final
        # duration directive.  FFmpeg framesync then holds it to the video end.
        concat_lines.append(f"file '{last_path.name}'")
        concat_path = overlay_root / "timeline.ffconcat"
        self._write_text(
            concat_path,
            "\n".join(concat_lines) + "\n",
            data_class="caption_overlay_timeline",
        )
        manifest_path = overlay_root / "manifest.json"
        self._write_text(
            manifest_path,
            json.dumps(
                {
                    "duration_seconds": duration,
                    "canvas_size": list(canvas_size),
                    "maximum_burned_caption_lines": 2,
                    "intervals": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            data_class="caption_overlay_manifest",
        )
        return concat_path

    def _render_text_overlay(
        self,
        output: Path,
        *,
        canvas_size: tuple[int, int],
        title: str | None,
        korean: str | None,
        english: str | None,
    ) -> None:
        Image, ImageDraw, ImageFont, _ = _pillow_modules()
        image = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        self._draw_text_overlay(
            image,
            title=title,
            korean=korean,
            english=english,
            thumbnail=False,
            image_draw=ImageDraw,
            image_font=ImageFont,
        )
        destination = self._output_path(output, data_class="caption_overlay_png")
        image.save(destination, format="PNG", optimize=True)

    def _draw_text_overlay(
        self,
        image,
        *,
        title: str | None,
        korean: str | None,
        english: str | None,
        thumbnail: bool,
        image_draw,
        image_font,
    ) -> None:
        width, height = image.size
        draw = image_draw.Draw(image, "RGBA")
        font_path = _korean_font_path()
        self.guard.authorize_path(font_path, operation="read", data_class="caption_font")

        def draw_boxed_text(
            text: str,
            *,
            nominal_size: int,
            center_y: float,
            maximum_width: int,
            spacing: int,
        ) -> None:
            size = nominal_size
            while True:
                font = image_font.truetype(str(font_path), size=size)
                bbox = draw.multiline_textbbox(
                    (0, 0), text, font=font, align="center", spacing=spacing, stroke_width=3
                )
                text_width = bbox[2] - bbox[0]
                if text_width <= maximum_width or size <= 24:
                    break
                size -= 2
            text_height = bbox[3] - bbox[1]
            padding_x = round(size * 0.45)
            padding_y = round(size * 0.30)
            left = (width - text_width) / 2 - padding_x
            top = center_y - text_height / 2 - padding_y
            right = (width + text_width) / 2 + padding_x
            bottom = center_y + text_height / 2 + padding_y
            draw.rounded_rectangle(
                (left, top, right, bottom),
                radius=max(12, round(size * 0.28)),
                fill=(0, 0, 0, 155 if thumbnail else 170),
            )
            draw.multiline_text(
                (width / 2, center_y),
                text,
                font=font,
                fill=(255, 255, 255, 255),
                anchor="mm",
                align="center",
                spacing=spacing,
                stroke_width=3,
                stroke_fill=(0, 0, 0, 230),
            )

        if title is not None:
            title_size = round(height * (0.075 if thumbnail else 0.055))
            draw_boxed_text(
                title,
                nominal_size=max(48, title_size),
                center_y=height * (0.50 if thumbnail else 0.095),
                maximum_width=round(width * 0.88),
                spacing=max(8, round(title_size * 0.22)),
            )
        if korean is not None and english is not None:
            caption_size = max(30, round(height * (0.034 if height >= 1500 else 0.043)))
            draw_boxed_text(
                f"{korean}\n{english}",
                nominal_size=caption_size,
                center_y=height * (0.865 if height >= 1500 else 0.89),
                maximum_width=round(width * 0.91),
                spacing=max(8, round(caption_size * 0.28)),
            )

    def _write_subtitle_artifacts(
        self,
        *,
        captions: tuple[BilingualCaptionCue, ...],
        duration: float,
        output: Path,
        canvas_size: tuple[int, int],
        title: str | None,
        title_end: float,
    ) -> VideoRenderArtifacts:
        self._validate_captions(captions, duration=duration)
        stem = output.with_suffix("")
        korean_srt = stem.with_name(f"{stem.name}.ko.srt")
        english_srt = stem.with_name(f"{stem.name}.en.srt")
        combined_ass = stem.with_name(f"{stem.name}.ass")
        self._write_srt(korean_srt, captions, language="korean")
        self._write_srt(english_srt, captions, language="english")
        self._write_ass(
            combined_ass,
            cues=captions,
            duration=duration,
            canvas_size=canvas_size,
            title=title,
            title_end=title_end,
        )
        return VideoRenderArtifacts(
            video=output,
            korean_srt=korean_srt,
            english_srt=english_srt,
            combined_ass=combined_ass,
            duration_seconds=duration,
        )

    def _write_srt(
        self,
        path: Path,
        cues: tuple[BilingualCaptionCue, ...],
        *,
        language: Literal["korean", "english"],
    ) -> None:
        lines: list[str] = []
        for index, cue in enumerate(cues, start=1):
            lines.extend(
                (
                    str(index),
                    f"{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}",
                    getattr(cue, language),
                    "",
                )
            )
        self._write_text(path, "\n".join(lines), data_class="subtitle_srt")

    def _write_ass(
        self,
        path: Path,
        *,
        cues: tuple[BilingualCaptionCue, ...],
        duration: float,
        canvas_size: tuple[int, int],
        title: str | None,
        title_end: float,
        thumbnail: bool = False,
    ) -> None:
        width, height = canvas_size
        caption_size = max(28, round(height * (0.030 if height >= 1500 else 0.041)))
        title_size = max(48, round(height * (0.048 if height >= 1500 else 0.060)))
        if thumbnail:
            title_size = max(64, round(height * 0.075))
        caption_margin = round(height * (0.095 if height >= 1500 else 0.065))
        title_margin = round(height * 0.055)
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Caption,Apple SD Gothic Neo,"
            f"{caption_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
            f"0,0,0,0,100,100,0,0,3,3,0,2,50,50,{caption_margin},1",
            "Style: Title,Apple SD Gothic Neo,"
            f"{title_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H52000000,"
            f"-1,0,0,0,100,100,0,0,3,4,1,{5 if thumbnail else 8},"
            f"70,70,{title_margin},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        if title is not None:
            lines.append(
                "Dialogue: 1,"
                f"{_format_ass_time(0)},{_format_ass_time(title_end)},Title,,0,0,0,,"
                f"{_escape_ass_text(title, allow_newlines=True)}"
            )
        for cue in cues:
            combined = (
                f"{_escape_ass_text(cue.korean)}\\N"
                f"{_escape_ass_text(cue.english)}"
            )
            lines.append(
                "Dialogue: 0,"
                f"{_format_ass_time(cue.start)},{_format_ass_time(cue.end)},"
                f"Caption,,0,0,0,,{combined}"
            )
        self._write_text(path, "\n".join(lines) + "\n", data_class="subtitle_ass")

    @staticmethod
    def _validate_title(title: str, *, maximum_lines: int) -> None:
        if not title.strip() or "\x00" in title:
            raise DogfoodRenderError("title_text_required")
        normalized = title.replace("\r\n", "\n").replace("\r", "\n")
        if len(normalized.split("\n")) > maximum_lines:
            raise DogfoodRenderError("title_exceeds_line_limit")

    @staticmethod
    def _validate_captions(
        cues: tuple[BilingualCaptionCue, ...], *, duration: float
    ) -> None:
        if not cues:
            raise DogfoodRenderError("bilingual_captions_required")
        previous_end = 0.0
        for cue in cues:
            if cue.start < 0 or cue.end <= cue.start or cue.end > duration + 0.05:
                raise DogfoodRenderError("caption_timing_invalid")
            if cue.start < previous_end - 0.001:
                raise DogfoodRenderError("caption_timing_overlaps")
            for language, text, limit in (
                ("korean", cue.korean, 42),
                ("english", cue.english, 64),
            ):
                if not text.strip():
                    raise DogfoodRenderError(f"{language}_caption_text_required")
                if any(character in text for character in ("\n", "\r", "\x00")):
                    raise DogfoodRenderError("combined_caption_must_be_two_lines")
                if len(text) > limit:
                    raise DogfoodRenderError(f"{language}_caption_line_too_long")
            previous_end = cue.end

    def _readable_file(self, path: Path | str, *, data_class: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        self.guard.authorize_path(resolved, operation="read", data_class=data_class)
        if not resolved.is_file() or resolved.stat().st_size == 0:
            raise DogfoodRenderError(f"{data_class}_missing_or_empty")
        return resolved

    def _output_path(self, path: Path | str, *, data_class: str) -> Path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = self.output_root / resolved
        resolved = resolved.resolve(strict=False)
        self.guard.authorize_path(resolved.parent, operation="write", data_class=data_class)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.guard.authorize_path(resolved, operation="write", data_class=data_class)
        return resolved

    def _write_text(self, path: Path, text: str, *, data_class: str) -> None:
        output = self._output_path(path, data_class=data_class)
        output.write_text(text, encoding="utf-8")

    def _probe(self, path: Path) -> _MediaProbe:
        self.guard.authorize_path(path, operation="read", data_class="media_probe_input")
        completed = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DogfoodRenderError("ffprobe_returned_invalid_json") from exc
        raw_duration = document.get("format", {}).get("duration")
        try:
            duration = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return _MediaProbe(duration, tuple(document.get("streams", ())))

    @staticmethod
    def _require_av_source(probe: _MediaProbe) -> None:
        types = {stream.get("codec_type") for stream in probe.streams}
        if "video" not in types:
            raise DogfoodRenderError("source_video_stream_required")
        if "audio" not in types:
            raise DogfoodRenderError("source_audio_stream_required")

    def _validate_video(
        self,
        path: Path,
        *,
        expected_size: tuple[int, int],
        minimum_duration: float,
        maximum_duration: float,
    ) -> float:
        probe = self._probe(path)
        stream_types = [stream.get("codec_type") for stream in probe.streams]
        if sorted(stream_types) != ["audio", "video"] or len(stream_types) != 2:
            raise DogfoodRenderError("output_must_have_only_one_video_and_audio_stream")
        video = next(stream for stream in probe.streams if stream.get("codec_type") == "video")
        audio = next(stream for stream in probe.streams if stream.get("codec_type") == "audio")
        if video.get("codec_name") != "h264":
            raise DogfoodRenderError("output_h264_required")
        if audio.get("codec_name") != "aac":
            raise DogfoodRenderError("output_aac_required")
        if (video.get("width"), video.get("height")) != expected_size:
            raise DogfoodRenderError("output_resolution_mismatch")
        if probe.duration is None or not (
            minimum_duration - 0.5 <= probe.duration <= maximum_duration + 0.5
        ):
            raise DogfoodRenderError("output_duration_mismatch")
        return probe.duration

    def _run(self, command: list[str]):
        try:
            return self.guard.run_tool(command)
        except FileNotFoundError as exc:
            raise DogfoodRenderError(f"missing_media_dependency:{command[0]}") from exc
        except (PermissionError, RuntimeError) as exc:
            raise DogfoodRenderError(str(exc)) from exc


def _decimal(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _format_srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def _format_ass_time(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{centis:02}"


def _escape_ass_text(text: str, *, allow_newlines: bool = False) -> str:
    """Escape exact Unicode text for an ASS dialogue payload."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not allow_newlines and "\n" in normalized:
        raise DogfoodRenderError("ass_dialogue_line_break_not_allowed")
    # ASS reserves backslash commands and brace-delimited override blocks.
    escaped = normalized.replace("\\", r"\\")
    escaped = escaped.replace("{", r"\{").replace("}", r"\}")
    return escaped.replace("\n", r"\N")


def _escape_ffmpeg_filter_value(value: str) -> str:
    """Escape one libavfilter option value (argv is already shell-free)."""

    if "\n" in value or "\r" in value or "\x00" in value:
        raise DogfoodRenderError("ffmpeg_filter_value_contains_control_character")
    reserved = set("\\':,[]; ")
    return "".join(f"\\{character}" if character in reserved else character for character in value)


def _pillow_modules():
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:
        raise DogfoodRenderError(
            "pillow_required_for_exact_text_rasterization"
        ) from exc
    return Image, ImageDraw, ImageFont, ImageOps


def _korean_font_path() -> Path:
    candidates = (
        Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
        Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DogfoodRenderError("korean_capable_font_required")
