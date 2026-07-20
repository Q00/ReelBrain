"""FFmpeg-backed local short-form and long-form package generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable, Literal


class MediaError(RuntimeError):
    pass


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise MediaError(f"missing_media_dependency:{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"media_command_failed:{exc.stderr.strip()}") from exc


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class MediaStream:
    codec_type: str
    codec_name: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_seconds: float
    streams: tuple[MediaStream, ...]

    @property
    def video_stream(self) -> MediaStream | None:
        return next((stream for stream in self.streams if stream.codec_type == "video"), None)

    @property
    def audio_stream(self) -> MediaStream | None:
        return next((stream for stream in self.streams if stream.codec_type == "audio"), None)


def probe_media(path: Path | str) -> MediaInfo:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise MediaError("source_media_missing_or_empty")
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(source),
        ]
    )
    document = json.loads(result.stdout)
    try:
        duration = float(document["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaError("source_duration_unavailable") from exc
    streams = tuple(
        MediaStream(
            codec_type=stream["codec_type"],
            codec_name=stream.get("codec_name", "unknown"),
            width=stream.get("width"),
            height=stream.get("height"),
        )
        for stream in document.get("streams", [])
    )
    if not any(stream.codec_type == "video" for stream in streams):
        raise MediaError("source_video_stream_required")
    if not any(stream.codec_type == "audio" for stream in streams):
        raise MediaError("source_audio_stream_required")
    return MediaInfo(source, duration, streams)


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    start: float
    end: float
    text: str
    thesis: str
    takeaway: str
    hook: str
    payoff: str
    required_context: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    confidence: float = 1.0
    educational_value: float = 1.0
    self_contained: bool = True
    complete_thought: bool = True
    must_keep: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class RightsEntry:
    asset_id: str
    source: str
    status: Literal["approved", "denied", "expired", "incompatible"]
    license_id: str
    permitted_uses: tuple[str, ...]
    attribution: str | None = None


@dataclass(frozen=True)
class PackagePaths:
    root: Path
    videos: tuple[Path, ...]
    captions_srt: Path
    captions_vtt: Path
    otio_timeline: Path
    asset_manifest: Path
    rights_manifest: Path
    traceability_map: Path
    audit_report: Path
    extras: dict[str, Path] = field(default_factory=dict)


def _format_srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _format_vtt_time(seconds: float) -> str:
    return _format_srt_time(seconds).replace(",", ".")


def write_captions(cues: Iterable[CaptionCue], srt_path: Path, vtt_path: Path) -> None:
    cue_list = tuple(cues)
    srt_lines: list[str] = []
    vtt_lines = ["WEBVTT", ""]
    for index, cue in enumerate(cue_list, start=1):
        if cue.start < 0 or cue.end <= cue.start:
            raise ValueError("invalid_caption_timing")
        lines = cue.text.splitlines()
        if len(lines) > 2:
            raise ValueError("caption_exceeds_two_lines")
        srt_lines.extend(
            [
                str(index),
                f"{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}",
                cue.text,
                "",
            ]
        )
        vtt_lines.extend(
            [
                f"{_format_vtt_time(cue.start)} --> {_format_vtt_time(cue.end)}",
                cue.text,
                "",
            ]
        )
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")


def word_error_rate(reference: str, hypothesis: str) -> float:
    reference_words = reference.lower().split()
    hypothesis_words = hypothesis.lower().split()
    if not reference_words:
        return 0.0 if not hypothesis_words else 1.0
    previous = list(range(len(hypothesis_words) + 1))
    for row, ref_word in enumerate(reference_words, start=1):
        current = [row]
        for column, hyp_word in enumerate(hypothesis_words, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_word != hyp_word),
                )
            )
        previous = current
    return previous[-1] / len(reference_words)


class LocalPackageBuilder:
    def __init__(self, *, ffmpeg_preset: str = "ultrafast", output_fps: int = 5) -> None:
        self.ffmpeg_preset = ffmpeg_preset
        self.output_fps = output_fps

    def _validate_source(
        self, source: Path | str, *, minimum_minutes: float, maximum_minutes: float = 60
    ) -> MediaInfo:
        info = probe_media(source)
        minutes = info.duration_seconds / 60
        if minutes < minimum_minutes or minutes > maximum_minutes:
            raise MediaError(
                f"source_duration_out_of_range:{minimum_minutes}-{maximum_minutes}_minutes"
            )
        if info.path.suffix.lower() not in {".mp4", ".mov", ".webm"}:
            raise MediaError("unsupported_source_container")
        return info

    def select_short_candidates(
        self, segments: Iterable[TranscriptSegment], *, count: int = 3
    ) -> tuple[TranscriptSegment, ...]:
        eligible = [
            segment
            for segment in segments
            if 30 <= segment.duration <= 60
            and segment.self_contained
            and segment.complete_thought
            and segment.confidence >= 0.7
        ]
        ranked = sorted(
            eligible,
            key=lambda segment: (
                segment.educational_value,
                segment.confidence,
                bool(segment.hook),
                bool(segment.payoff),
            ),
            reverse=True,
        )
        selected: list[TranscriptSegment] = []
        takeaway_ids: set[str] = set()
        for segment in ranked:
            if segment.takeaway in takeaway_ids:
                continue
            if any(
                max(0.0, min(segment.end, other.end) - max(segment.start, other.start))
                / min(segment.duration, other.duration)
                > 0.2
                for other in selected
            ):
                continue
            selected.append(segment)
            takeaway_ids.add(segment.takeaway)
            if len(selected) == count:
                break
        if len(selected) != count:
            raise MediaError("insufficient_diverse_short_candidates")
        return tuple(selected)

    def build_short_package(
        self,
        *,
        source: Path | str,
        segments: Iterable[TranscriptSegment],
        output_dir: Path | str,
        project_id: str,
        creator_id: str,
        rights: Iterable[RightsEntry],
        approved_thumbnail: bool = False,
    ) -> PackagePaths:
        info = self._validate_source(source, minimum_minutes=5)
        rights_entries = self._validate_rights(rights, required_use="short_form_export")
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        candidates = self.select_short_candidates(segments)
        videos: list[Path] = []
        for index, segment in enumerate(candidates, start=1):
            output = root / f"candidate_{index}.mp4"
            self._render_clip(
                source=info.path,
                start=segment.start,
                duration=segment.duration,
                output=output,
                width=1080,
                height=1920,
            )
            self._validate_output(output, width=1080, height=1920, minimum=30, maximum=60)
            videos.append(output)
        final = root / "final_short.mp4"
        shutil.copy2(videos[0], final)
        videos.append(final)

        selected = candidates[0]
        cue = CaptionCue(0, selected.duration, selected.text)
        srt = root / "captions.srt"
        vtt = root / "captions.vtt"
        write_captions((cue,), srt, vtt)
        otio = root / "timeline.otio"
        self._write_json(otio, self._otio_document(candidates, kind="short"))
        traceability = root / "source_traceability.json"
        self._write_json(
            traceability,
            {
                "project_id": project_id,
                "candidates": [asdict(segment) for segment in candidates],
            },
        )
        value_cards = root / "educational_value_cards.json"
        self._write_json(
            value_cards,
            [
                {
                    "segment_id": segment.segment_id,
                    "thesis": segment.thesis,
                    "takeaway": segment.takeaway,
                    "hook": segment.hook,
                    "payoff": segment.payoff,
                    "source_range": [segment.start, segment.end],
                    "risks": list(segment.risks),
                }
                for segment in candidates
            ],
        )
        rights_manifest = root / "rights_manifest.json"
        self._write_json(rights_manifest, [asdict(entry) for entry in rights_entries])
        asset_manifest = root / "asset_manifest.json"
        self._write_json(
            asset_manifest,
            self._asset_manifest(project_id, creator_id, info.path, videos, (srt, vtt, otio)),
        )
        audit = root / "validation_report.json"
        self._write_json(
            audit,
            {
                "status": "AUTO_VERIFIED",
                "output_mode": "short",
                "candidate_count": 3,
                "diverse_takeaways": len({segment.takeaway for segment in candidates}) == 3,
                "complete_thoughts": all(segment.complete_thought for segment in candidates),
                "self_contained": all(segment.self_contained for segment in candidates),
                "source_digest": file_digest(info.path),
            },
        )
        extras = {"educational_value_cards": value_cards, "final_video": final}
        if approved_thumbnail:
            thumbnail = root / "thumbnail.jpg"
            self._thumbnail(info.path, selected.start + 0.5, thumbnail, width=1080, height=1920)
            extras["thumbnail"] = thumbnail
            metadata = root / "metadata_draft.json"
            self._write_json(
                metadata,
                {
                    "title": selected.hook,
                    "description": selected.takeaway,
                    "creator_approved_thumbnail": True,
                },
            )
            extras["metadata_draft"] = metadata
        return PackagePaths(
            root=root,
            videos=tuple(videos),
            captions_srt=srt,
            captions_vtt=vtt,
            otio_timeline=otio,
            asset_manifest=asset_manifest,
            rights_manifest=rights_manifest,
            traceability_map=traceability,
            audit_report=audit,
            extras=extras,
        )

    def build_long_package(
        self,
        *,
        source: Path | str,
        argument_map: Iterable[TranscriptSegment],
        output_dir: Path | str,
        project_id: str,
        creator_id: str,
        rights: Iterable[RightsEntry],
        corrected_transcript: str,
        creator_approval_receipt: str,
        cost_receipt: dict[str, object],
    ) -> PackagePaths:
        info = self._validate_source(source, minimum_minutes=20)
        if not creator_approval_receipt.strip():
            raise ValueError("creator_approval_receipt_required")
        rights_entries = self._validate_rights(rights, required_use="long_form_export")
        segments = tuple(sorted(argument_map, key=lambda item: item.start))
        total_duration = sum(segment.duration for segment in segments)
        if not 300 <= total_duration <= 720:
            raise MediaError("long_form_duration_must_be_5_to_12_minutes")
        if not segments or not all(segment.complete_thought for segment in segments):
            raise MediaError("argument_map_incomplete")
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        output = root / "final_long.mp4"
        self._render_sequence(info.path, segments, output, width=1920, height=1080)
        self._validate_output(output, width=1920, height=1080, minimum=300, maximum=720)

        cues: list[CaptionCue] = []
        offset = 0.0
        for segment in segments:
            cues.append(CaptionCue(offset, offset + segment.duration, segment.text))
            offset += segment.duration
        srt = root / "captions.srt"
        vtt = root / "captions.vtt"
        write_captions(cues, srt, vtt)
        chapters = root / "chapters.json"
        offset = 0.0
        chapter_rows = []
        for index, segment in enumerate(segments, start=1):
            chapter_rows.append(
                {"index": index, "start": offset, "title": segment.thesis, "segment_id": segment.segment_id}
            )
            offset += segment.duration
        self._write_json(chapters, chapter_rows)
        thumbnail = root / "thumbnail.jpg"
        self._thumbnail(info.path, segments[0].start + 1, thumbnail, width=1920, height=1080)
        otio = root / "timeline.otio"
        self._write_json(otio, self._otio_document(segments, kind="long"))
        argument_path = root / "argument_map.json"
        self._write_json(argument_path, [asdict(segment) for segment in segments])
        traceability = root / "source_traceability.json"
        self._write_json(
            traceability,
            {
                "project_id": project_id,
                "argument_order": [segment.segment_id for segment in segments],
                "source_ranges": {
                    segment.segment_id: [segment.start, segment.end] for segment in segments
                },
            },
        )
        transcript_path = root / "corrected_transcript.txt"
        transcript_path.write_text(corrected_transcript, encoding="utf-8")
        rights_manifest = root / "rights_manifest.json"
        self._write_json(rights_manifest, [asdict(entry) for entry in rights_entries])
        render_recipe = root / "render_recipe.json"
        self._write_json(
            render_recipe,
            {
                "container": "mp4",
                "video_codec": "h264",
                "audio_codec": "aac",
                "resolution": [1920, 1080],
                "segments": [[segment.start, segment.end] for segment in segments],
            },
        )
        provenance = root / "provenance.json"
        self._write_json(
            provenance,
            {"source_digest": file_digest(info.path), "renderer": "ffmpeg", "mode": "local"},
        )
        cost_path = root / "cost_receipt.json"
        self._write_json(cost_path, cost_receipt)
        approval_path = root / "approval_history.json"
        self._write_json(approval_path, [{"receipt": creator_approval_receipt, "action": "export_approved"}])
        asset_manifest = root / "asset_manifest.json"
        self._write_json(
            asset_manifest,
            self._asset_manifest(
                project_id,
                creator_id,
                info.path,
                (output,),
                (srt, vtt, chapters, thumbnail, otio, argument_path),
            ),
        )
        audit = root / "validation_report.json"
        self._write_json(
            audit,
            {
                "status": "PUBLISH_READY",
                "output_mode": "long",
                "argument_map_preserved": True,
                "complete_thoughts": True,
                "creator_approval_receipt": creator_approval_receipt,
                "duration_seconds": total_duration,
            },
        )
        return PackagePaths(
            root=root,
            videos=(output,),
            captions_srt=srt,
            captions_vtt=vtt,
            otio_timeline=otio,
            asset_manifest=asset_manifest,
            rights_manifest=rights_manifest,
            traceability_map=traceability,
            audit_report=audit,
            extras={
                "chapters": chapters,
                "thumbnail": thumbnail,
                "render_recipe": render_recipe,
                "argument_map": argument_path,
                "corrected_transcript": transcript_path,
                "provenance": provenance,
                "cost_receipt": cost_path,
                "approval_history": approval_path,
            },
        )

    def _render_clip(
        self,
        *,
        source: Path,
        start: float,
        duration: float,
        output: Path,
        width: int,
        height: int,
    ) -> None:
        filter_graph = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(duration),
                "-vf",
                filter_graph,
                "-r",
                str(self.output_fps),
                "-c:v",
                "libx264",
                "-preset",
                self.ffmpeg_preset,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )

    def _render_sequence(
        self,
        source: Path,
        segments: tuple[TranscriptSegment, ...],
        output: Path,
        *,
        width: int,
        height: int,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="reelbrain-render-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            clips: list[Path] = []
            for index, segment in enumerate(segments):
                clip = temp_dir / f"clip_{index:03}.mp4"
                self._render_clip(
                    source=source,
                    start=segment.start,
                    duration=segment.duration,
                    output=clip,
                    width=width,
                    height=height,
                )
                clips.append(clip)
            concat_list = temp_dir / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8"
            )
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )

    def _thumbnail(
        self, source: Path, at: float, output: Path, *, width: int, height: int
    ) -> None:
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(at),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
                str(output),
            ]
        )

    def _validate_output(
        self, path: Path, *, width: int, height: int, minimum: float, maximum: float
    ) -> None:
        info = probe_media(path)
        video = info.video_stream
        audio = info.audio_stream
        if video is None or video.codec_name != "h264":
            raise MediaError("output_h264_required")
        if audio is None or audio.codec_name != "aac":
            raise MediaError("output_aac_required")
        if (video.width, video.height) != (width, height):
            raise MediaError("output_resolution_mismatch")
        if not minimum - 0.5 <= info.duration_seconds <= maximum + 0.5:
            raise MediaError("output_duration_mismatch")

    @staticmethod
    def _validate_rights(
        rights: Iterable[RightsEntry], *, required_use: str
    ) -> tuple[RightsEntry, ...]:
        entries = tuple(rights)
        if not entries:
            raise ValueError("rights_manifest_required")
        for entry in entries:
            if entry.status != "approved" or required_use not in entry.permitted_uses:
                raise PermissionError("rights_do_not_permit_export")
        return entries

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _otio_document(segments: Iterable[TranscriptSegment], *, kind: str) -> dict[str, object]:
        return {
            "OTIO_SCHEMA": "Timeline.1",
            "name": f"ReelBrain {kind} timeline",
            "tracks": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "kind": "Video",
                    "children": [
                        {
                            "OTIO_SCHEMA": "Clip.2",
                            "name": segment.segment_id,
                            "source_range": {
                                "start_time": segment.start,
                                "duration": segment.duration,
                            },
                        }
                        for segment in segments
                    ],
                }
            ],
        }

    @staticmethod
    def _asset_manifest(
        project_id: str,
        creator_id: str,
        source: Path,
        videos: Iterable[Path],
        supporting: Iterable[Path],
    ) -> dict[str, object]:
        paths = (source, *tuple(videos), *tuple(supporting))
        return {
            "project_id": project_id,
            "creator_id": creator_id,
            "assets": [
                {"path": str(path), "sha256": file_digest(path), "bytes": path.stat().st_size}
                for path in paths
            ],
        }

