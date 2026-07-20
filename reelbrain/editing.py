"""FFmpeg-backed local short-form and long-form package generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import textwrap
import subprocess
import tempfile
from typing import Iterable, Literal

from .runtime_guard import RuntimeGuard


class MediaError(RuntimeError):
    pass


def run_command(
    command: list[str], guard: RuntimeGuard
) -> subprocess.CompletedProcess[str]:
    try:
        return guard.run_tool(command)
    except FileNotFoundError as exc:
        raise MediaError(f"missing_media_dependency:{command[0]}") from exc
    except RuntimeError as exc:
        raise MediaError(str(exc)) from exc


def file_digest(path: Path, guard: RuntimeGuard) -> str:
    guard.authorize_path(path, operation="read", data_class="media_or_artifact")
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


def probe_media(path: Path | str, guard: RuntimeGuard | None = None) -> MediaInfo:
    source = Path(path).expanduser().resolve()
    active_guard = guard or RuntimeGuard(
        workspace_root=source.parent,
        project_id="standalone-media-probe",
        creator_id="local-creator",
        tool_names=("ffprobe",),
    )
    active_guard.authorize_path(source, operation="read", data_class="source_media")
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
        ],
        active_guard,
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
class CaptionValidation:
    reference_kind: str
    reference_confidence: float
    highlight_word_error_rate: float
    caption_word_error_rate: float
    meaning_changing_caption_errors: int
    timing_usable: bool
    layout_passed: bool

    @property
    def passed(self) -> bool:
        return (
            self.reference_kind != "self_attested"
            and self.reference_confidence >= 0.8
            and self.highlight_word_error_rate <= 0.05
            and self.caption_word_error_rate <= 0.05
            and self.meaning_changing_caption_errors == 0
            and self.timing_usable
            and self.layout_passed
        )


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


def write_captions(
    cues: Iterable[CaptionCue],
    srt_path: Path,
    vtt_path: Path,
    guard: RuntimeGuard,
) -> None:
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
    guard.authorize_path(srt_path, operation="write", data_class="captions")
    guard.authorize_path(vtt_path, operation="write", data_class="captions")
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


def word_edit_distance(reference: str, hypothesis: str) -> int:
    reference_words = reference.lower().split()
    hypothesis_words = hypothesis.lower().split()
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
    return previous[-1]


def caption_cues(
    text: str,
    duration: float,
    *,
    max_chars_per_line: int = 32,
    max_cue_seconds: float = 6.0,
) -> tuple[CaptionCue, ...]:
    """Create readable, proportionally timed cues with at most two lines."""

    words = text.split()
    if not words or duration <= 0:
        raise ValueError("caption_text_and_duration_required")
    minimum_cues = max(1, math.ceil(duration / max_cue_seconds))
    target_words = max(1, math.ceil(len(words) / minimum_cues))
    groups: list[list[str]] = []
    current: list[str] = []
    for word in words:
        candidate = [*current, word]
        wrapped = textwrap.wrap(" ".join(candidate), width=max_chars_per_line)
        if current and (len(candidate) > target_words or len(wrapped) > 2):
            groups.append(current)
            current = [word]
        else:
            current = candidate
    if current:
        groups.append(current)

    total_words = sum(len(group) for group in groups)
    cues: list[CaptionCue] = []
    elapsed = 0.0
    for index, group in enumerate(groups):
        cue_duration = duration * len(group) / total_words
        end = duration if index == len(groups) - 1 else elapsed + cue_duration
        lines = textwrap.wrap(" ".join(group), width=max_chars_per_line)
        cues.append(CaptionCue(elapsed, end, "\n".join(lines)))
        elapsed = end
    return tuple(cues)


def validate_captions(
    *,
    source_reference: str,
    highlight_text: str,
    cues: Iterable[CaptionCue],
    reference_kind: str,
    reference_confidence: float,
) -> CaptionValidation:
    cue_list = tuple(cues)
    caption_text = " ".join(cue.text.replace("\n", " ") for cue in cue_list)
    timing_usable = bool(cue_list) and all(
        cue.start >= 0
        and cue.end > cue.start
        and cue.end - cue.start <= 6.01
        and (index == 0 or cue.start >= cue_list[index - 1].end - 0.001)
        for index, cue in enumerate(cue_list)
    )
    layout_passed = all(
        len(cue.text.splitlines()) <= 2
        and all(len(line) <= 32 for line in cue.text.splitlines())
        for cue in cue_list
    )
    return CaptionValidation(
        reference_kind=reference_kind,
        reference_confidence=reference_confidence,
        highlight_word_error_rate=word_error_rate(source_reference, highlight_text),
        caption_word_error_rate=word_error_rate(source_reference, caption_text),
        # Conservatively treat every transcript/caption word edit as meaning-bearing
        # until a creator-corrected or gold reference proves otherwise.
        meaning_changing_caption_errors=word_edit_distance(source_reference, caption_text),
        timing_usable=timing_usable,
        layout_passed=layout_passed,
    )


class LocalPackageBuilder:
    def __init__(self, *, ffmpeg_preset: str = "ultrafast", output_fps: int = 5) -> None:
        self.ffmpeg_preset = ffmpeg_preset
        self.output_fps = output_fps
        self.guard: RuntimeGuard | None = None

    def _begin_guard(
        self,
        *,
        source: Path | str,
        output_dir: Path | str,
        project_id: str,
        creator_id: str,
    ) -> tuple[Path, Path]:
        source_path = Path(source).expanduser().resolve()
        root = Path(output_dir).expanduser().resolve()
        self.guard = RuntimeGuard(
            workspace_root=root,
            local_allowlist=(source_path.parent,),
            project_id=project_id,
            creator_id=creator_id,
            tool_names=("ffmpeg", "ffprobe"),
        )
        self.guard.authorize_path(root, operation="write", data_class="project_output")
        self.guard.authorize_path(source_path, operation="read", data_class="source_media")
        return source_path, root

    def _require_guard(self) -> RuntimeGuard:
        if self.guard is None:
            raise RuntimeError("runtime_guard_not_initialized")
        return self.guard

    def _validate_source(
        self, source: Path | str, *, minimum_minutes: float, maximum_minutes: float = 60
    ) -> MediaInfo:
        info = probe_media(source, self._require_guard())
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

    @staticmethod
    def segments_from_transcript_chunks(chunks: Iterable[object]) -> tuple[TranscriptSegment, ...]:
        """Combine normal short STT chunks into complete 30-60 second windows."""

        ordered = tuple(sorted(chunks, key=lambda chunk: chunk.start))
        windows: list[TranscriptSegment] = []
        current: list[object] = []
        for chunk in ordered:
            if current and chunk.start - current[-1].end > 2:
                current = []
            current.append(chunk)
            duration = current[-1].end - current[0].start
            if duration < 30:
                continue
            if duration > 60:
                current = [chunk]
                continue
            text = " ".join(item.text.strip() for item in current).strip()
            if not text.endswith((".", "!", "?")) and duration < 55:
                continue
            window_id = f"window-{current[0].chunk_id}-{current[-1].chunk_id}"
            confidence = sum(item.confidence for item in current) / len(current)
            windows.append(
                TranscriptSegment(
                    segment_id=window_id,
                    start=current[0].start,
                    end=current[-1].end,
                    text=text,
                    thesis=text.split(".", 1)[0].strip(),
                    takeaway=text,
                    hook=" ".join(text.split()[:8]),
                    payoff="Complete educational explanation",
                    confidence=confidence,
                    educational_value=confidence,
                    self_contained=text.endswith((".", "!", "?")),
                    complete_thought=text.endswith((".", "!", "?")),
                )
            )
            current = []
        return tuple(windows)

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
        creator_approval_receipt: str | None = None,
        caption_reference: str | None = None,
        caption_reference_kind: str = "self_attested",
        caption_reference_confidence: float = 0.0,
    ) -> PackagePaths:
        source_path, root = self._begin_guard(
            source=source,
            output_dir=output_dir,
            project_id=project_id,
            creator_id=creator_id,
        )
        info = self._validate_source(source_path, minimum_minutes=5)
        rights_entries = self._validate_rights(rights, required_use="short_form_export")
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
        guard = self._require_guard()
        guard.authorize_path(videos[0], operation="read", data_class="rendered_video")
        guard.authorize_path(final, operation="write", data_class="rendered_video")
        shutil.copy2(videos[0], final)
        videos.append(final)

        selected = candidates[0]
        cues = caption_cues(selected.text, selected.duration)
        caption_validation = validate_captions(
            source_reference=caption_reference or selected.text,
            highlight_text=selected.text,
            cues=cues,
            reference_kind=caption_reference_kind,
            reference_confidence=caption_reference_confidence,
        )
        srt = root / "captions.srt"
        vtt = root / "captions.vtt"
        write_captions(cues, srt, vtt, guard)
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
                "status": (
                    "PUBLISH_READY"
                    if creator_approval_receipt and caption_validation.passed
                    else "CREATOR_REVIEW"
                ),
                "output_mode": "short",
                "candidate_count": 3,
                "diverse_takeaways": len({segment.takeaway for segment in candidates}) == 3,
                "complete_thoughts": all(segment.complete_thought for segment in candidates),
                "self_contained": all(segment.self_contained for segment in candidates),
                "source_digest": file_digest(info.path, guard),
                "creator_approval_receipt": creator_approval_receipt,
                "caption_validation": asdict(caption_validation),
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
        governance_artifacts = guard.write_audit_artifacts(
            root / "governance", rights_manifest=[asdict(entry) for entry in rights_entries]
        )
        extras.update({f"governance_{key}": path for key, path in governance_artifacts.items()})
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

    def build_short_from_video(
        self,
        *,
        source: Path | str,
        stt_provider,
        output_dir: Path | str,
        project_id: str,
        creator_id: str,
        rights: Iterable[RightsEntry],
        creator_approval_receipt: str,
        preferred_terms: Iterable[str] = (),
        approved_thumbnail: bool = False,
        provider_consent_receipt: dict[str, object] | None = None,
        budget_reservation_receipt: dict[str, object] | None = None,
    ) -> PackagePaths:
        """Ingest one video, transcribe, fan out highlight scouts, and package."""

        from .agents import HighlightAgentTeam

        rights_entries = tuple(rights)
        source_path, _ = self._begin_guard(
            source=source,
            output_dir=output_dir,
            project_id=project_id,
            creator_id=creator_id,
        )
        info = self._validate_source(source_path, minimum_minutes=5)
        chunks = tuple(
            self._require_guard().run_callback_tool(
                tool_id=stt_provider.name,
                capability="stt:transcribe",
                dispatch=lambda: stt_provider.transcribe(info.path),
                official=bool(getattr(stt_provider, "official", False)),
                provider=getattr(stt_provider, "provider", None),
                consent_receipt=provider_consent_receipt,
                destination_host=getattr(stt_provider, "destination_host", None),
                budget_reservation_receipt=budget_reservation_receipt,
            )
        )
        stt_capability_receipts = list(self._require_guard().capability_receipts)
        stt_provider_receipts = list(self._require_guard().provider_receipts)
        stt_denials = list(self._require_guard().denial_logs)
        if not chunks:
            raise MediaError("stt_returned_no_transcript")
        candidates = self.segments_from_transcript_chunks(chunks)
        selected, assessments = HighlightAgentTeam(preferred_terms=preferred_terms).select(
            candidates, count=3
        )
        selected_reference_chunks = tuple(
            chunk
            for chunk in chunks
            if chunk.start < selected[0].end and chunk.end > selected[0].start
        )
        source_reference = " ".join(
            chunk.text.strip() for chunk in selected_reference_chunks
        ).strip()
        if not source_reference:
            raise MediaError("selected_highlight_source_reference_missing")
        package = self.build_short_package(
            source=info.path,
            segments=selected,
            output_dir=output_dir,
            project_id=project_id,
            creator_id=creator_id,
            rights=rights_entries,
            approved_thumbnail=approved_thumbnail,
            creator_approval_receipt=creator_approval_receipt,
            caption_reference=source_reference,
            caption_reference_kind=getattr(
                stt_provider, "reference_kind", "source_stt_alignment"
            ),
            caption_reference_confidence=min(
                chunk.confidence for chunk in selected_reference_chunks
            ),
        )
        self._require_guard().capability_receipts[0:0] = stt_capability_receipts
        self._require_guard().provider_receipts[0:0] = stt_provider_receipts
        self._require_guard().denial_logs[0:0] = stt_denials
        refreshed_governance = self._require_guard().write_audit_artifacts(
            package.root / "governance",
            rights_manifest=[asdict(entry) for entry in rights_entries],
        )
        transcript_artifact = package.root / "source_transcript.json"
        self._write_json(transcript_artifact, [asdict(chunk) for chunk in chunks])
        assessments_artifact = package.root / "agent_assessments.json"
        self._write_json(assessments_artifact, [asdict(item) for item in assessments])
        audit_document = json.loads(package.audit_report.read_text(encoding="utf-8"))
        caption_validation = audit_document["caption_validation"]
        audit_document.update(
            {
                "stt_provider": stt_provider.name,
                "highlight_discovery": "agent_fan_out",
                "source_faithful": (
                    caption_validation["highlight_word_error_rate"] <= 0.05
                    and caption_validation["caption_word_error_rate"] <= 0.05
                ),
                "meaning_changing_caption_errors": caption_validation[
                    "meaning_changing_caption_errors"
                ],
            }
        )
        self._write_json(package.audit_report, audit_document)
        return PackagePaths(
            root=package.root,
            videos=package.videos,
            captions_srt=package.captions_srt,
            captions_vtt=package.captions_vtt,
            otio_timeline=package.otio_timeline,
            asset_manifest=package.asset_manifest,
            rights_manifest=package.rights_manifest,
            traceability_map=package.traceability_map,
            audit_report=package.audit_report,
            extras={
                **package.extras,
                **{
                    f"governance_{key}": path
                    for key, path in refreshed_governance.items()
                },
                "source_transcript": transcript_artifact,
                "agent_assessments": assessments_artifact,
            },
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
        source_path, root = self._begin_guard(
            source=source,
            output_dir=output_dir,
            project_id=project_id,
            creator_id=creator_id,
        )
        info = self._validate_source(source_path, minimum_minutes=20)
        if not creator_approval_receipt.strip():
            raise ValueError("creator_approval_receipt_required")
        rights_entries = self._validate_rights(rights, required_use="long_form_export")
        # The creator-confirmed argument-map order is authoritative, even when it
        # intentionally differs from source chronology.
        segments = tuple(argument_map)
        total_duration = sum(segment.duration for segment in segments)
        if not 300 <= total_duration <= 720:
            raise MediaError("long_form_duration_must_be_5_to_12_minutes")
        if not segments or not all(segment.complete_thought for segment in segments):
            raise MediaError("argument_map_incomplete")
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
        write_captions(cues, srt, vtt, self._require_guard())
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
        self._require_guard().authorize_path(
            transcript_path, operation="write", data_class="transcript"
        )
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
            {
                "source_digest": file_digest(info.path, self._require_guard()),
                "renderer": "ffmpeg",
                "mode": "local",
            },
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
        governance_artifacts = self._require_guard().write_audit_artifacts(
            root / "governance", rights_manifest=[asdict(entry) for entry in rights_entries]
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
                **{f"governance_{key}": path for key, path in governance_artifacts.items()},
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
        guard = self._require_guard()
        guard.authorize_path(source, operation="read", data_class="source_media")
        guard.authorize_path(output, operation="write", data_class="rendered_video")
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
            ],
            guard,
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
        with tempfile.TemporaryDirectory(
            prefix="reelbrain-render-", dir=self._require_guard().workspace_root
        ) as temp_dir_name:
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
            self._require_guard().authorize_path(
                concat_list, operation="write", data_class="render_recipe"
            )
            concat_list.write_text(
                "\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8"
            )
            self._require_guard().authorize_path(
                output, operation="write", data_class="rendered_video"
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
                ],
                self._require_guard(),
            )

    def _thumbnail(
        self, source: Path, at: float, output: Path, *, width: int, height: int
    ) -> None:
        self._require_guard().authorize_path(source, operation="read", data_class="source_media")
        self._require_guard().authorize_path(output, operation="write", data_class="thumbnail")
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
            ],
            self._require_guard(),
        )

    def _validate_output(
        self, path: Path, *, width: int, height: int, minimum: float, maximum: float
    ) -> None:
        info = probe_media(path, self._require_guard())
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

    def _write_json(self, path: Path, value: object) -> None:
        self._require_guard().authorize_path(path, operation="write", data_class="json_artifact")
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

    def _asset_manifest(
        self,
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
                {
                    "path": str(path),
                    "sha256": file_digest(path, self._require_guard()),
                    "bytes": path.stat().st_size,
                }
                for path in paths
            ],
        }
