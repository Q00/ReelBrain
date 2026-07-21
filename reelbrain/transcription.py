"""Selectable local and consent-gated speech-to-text adapters."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import mimetypes
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .editing import MediaError, run_command
from .runtime_guard import RuntimeGuard


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: str
    start: float
    end: float
    text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class SpeechWindow:
    """A source-timeline interval containing the useful spoken content."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class BilingualTranscript:
    """Timestamp-aligned Korean source speech and its English translation."""

    korean: tuple[TranscriptChunk, ...]
    english: tuple[TranscriptChunk, ...]
    speech_windows: tuple[SpeechWindow, ...]
    original_language: str = "ko"
    translation_language: str = "en"
    provider: str = "openai"
    model: str = "whisper-1"


class STTProvider(Protocol):
    name: str

    def transcribe(self, video_path: Path) -> tuple[TranscriptChunk, ...]: ...


class SpeechWindowDetector(Protocol):
    def detect(self, source: Path, guard: RuntimeGuard) -> tuple[SpeechWindow, ...]: ...


class AudioChunkExtractor(Protocol):
    def extract(
        self,
        *,
        source: Path,
        start: float,
        end: float,
        destination: Path,
        guard: RuntimeGuard,
    ) -> None: ...


class WhisperTransport(Protocol):
    def transcribe(
        self,
        *,
        api_key: str,
        audio_path: Path,
        model: str,
        language: str,
    ) -> Mapping[str, object]: ...

    def translate(
        self,
        *,
        api_key: str,
        audio_path: Path,
        model: str,
    ) -> Mapping[str, object]: ...


_SRT_TIME = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_VTT_TIME = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)


def _subtitle_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid_subtitle_timestamp")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


_SILENCE_START = re.compile(r"silence_start:\s*(?P<time>\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(?P<time>\d+(?:\.\d+)?)")


class FFmpegSpeechWindowDetector:
    """Trim only silent outer tails while retaining source-timeline offsets."""

    def __init__(
        self,
        *,
        noise_threshold_db: float = -35.0,
        minimum_silence_seconds: float = 1.0,
        edge_tolerance_seconds: float = 0.1,
    ) -> None:
        if minimum_silence_seconds <= 0:
            raise ValueError("minimum_silence_seconds_must_be_positive")
        self.noise_threshold_db = noise_threshold_db
        self.minimum_silence_seconds = minimum_silence_seconds
        self.edge_tolerance_seconds = edge_tolerance_seconds

    def detect(self, source: Path, guard: RuntimeGuard) -> tuple[SpeechWindow, ...]:
        from .editing import probe_media

        source = Path(source).expanduser().resolve()
        info = probe_media(source, guard)
        result = run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(source),
                "-vn",
                "-af",
                (
                    f"silencedetect=noise={self.noise_threshold_db:g}dB:"
                    f"duration={self.minimum_silence_seconds:g}"
                ),
                "-f",
                "null",
                "-",
            ],
            guard,
        )
        intervals = self._silence_intervals(result.stderr, info.duration_seconds)
        content_start = 0.0
        content_end = info.duration_seconds
        if intervals and intervals[0][0] <= self.edge_tolerance_seconds:
            content_start = intervals[0][1]
        if intervals and intervals[-1][1] >= info.duration_seconds - self.edge_tolerance_seconds:
            content_end = intervals[-1][0]
        if content_end <= content_start:
            raise MediaError("source_contains_no_detectable_speech")
        return (SpeechWindow(content_start, content_end),)

    @staticmethod
    def _silence_intervals(stderr: str, duration: float) -> tuple[tuple[float, float], ...]:
        intervals: list[tuple[float, float]] = []
        open_start: float | None = None
        for line in stderr.splitlines():
            start_match = _SILENCE_START.search(line)
            if start_match is not None:
                open_start = float(start_match.group("time"))
            end_match = _SILENCE_END.search(line)
            if end_match is not None and open_start is not None:
                end = min(duration, float(end_match.group("time")))
                if end > open_start:
                    intervals.append((max(0.0, open_start), end))
                open_start = None
        if open_start is not None and duration > open_start:
            intervals.append((max(0.0, open_start), duration))
        return tuple(intervals)


class FFmpegAudioChunkExtractor:
    """Extract compact mono audio chunks suitable for the Whisper upload limit."""

    def __init__(self, *, bitrate: str = "64k", sample_rate: int = 16_000) -> None:
        self.bitrate = bitrate
        self.sample_rate = sample_rate

    def extract(
        self,
        *,
        source: Path,
        start: float,
        end: float,
        destination: Path,
        guard: RuntimeGuard,
    ) -> None:
        if end <= start:
            raise MediaError("audio_chunk_range_invalid")
        guard.authorize_path(source, operation="read", data_class="source_media")
        guard.authorize_path(destination, operation="write", data_class="provider_audio_chunk")
        destination.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(source),
                "-t",
                f"{end - start:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(self.sample_rate),
                "-b:a",
                self.bitrate,
                "-y",
                str(destination),
            ],
            guard,
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MediaError("audio_chunk_extraction_failed")


class OpenAIWhisperHTTPTransport:
    """Minimal fixed-destination multipart transport for OpenAI Whisper."""

    transcription_endpoint = "https://api.openai.com/v1/audio/transcriptions"
    translation_endpoint = "https://api.openai.com/v1/audio/translations"
    maximum_file_bytes = 25 * 1024 * 1024

    def transcribe(
        self,
        *,
        api_key: str,
        audio_path: Path,
        model: str,
        language: str,
    ) -> Mapping[str, object]:
        return self._post_multipart(
            endpoint=self.transcription_endpoint,
            api_key=api_key,
            audio_path=audio_path,
            fields=(
                ("model", model),
                ("language", language),
                ("response_format", "verbose_json"),
                ("timestamp_granularities[]", "segment"),
            ),
        )

    def translate(
        self,
        *,
        api_key: str,
        audio_path: Path,
        model: str,
    ) -> Mapping[str, object]:
        return self._post_multipart(
            endpoint=self.translation_endpoint,
            api_key=api_key,
            audio_path=audio_path,
            fields=(
                ("model", model),
                ("response_format", "verbose_json"),
            ),
        )

    def _post_multipart(
        self,
        *,
        endpoint: str,
        api_key: str,
        audio_path: Path,
        fields: Sequence[tuple[str, str]],
    ) -> Mapping[str, object]:
        if not api_key:
            raise PermissionError("openai_api_key_required")
        size = audio_path.stat().st_size
        if size > self.maximum_file_bytes:
            raise MediaError("openai_audio_file_exceeds_25mb")
        boundary = f"reelbrain-{uuid4().hex}"
        body = self._multipart_body(boundary, audio_path, fields)
        request = Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=180) as response:  # noqa: S310 - fixed endpoints
                document = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"openai_whisper_http_error:{exc.code}") from None
        except URLError:
            raise RuntimeError("openai_whisper_transport_unavailable") from None
        if not isinstance(document, dict):
            raise RuntimeError("openai_whisper_response_invalid")
        return document

    @staticmethod
    def _multipart_body(
        boundary: str,
        audio_path: Path,
        fields: Sequence[tuple[str, str]],
    ) -> bytes:
        marker = boundary.encode("ascii")
        body = bytearray()
        for name, value in fields:
            body.extend(b"--" + marker + b"\r\n")
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(value.encode("utf-8") + b"\r\n")
        content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
        safe_name = audio_path.name.replace('"', "")
        body.extend(b"--" + marker + b"\r\n")
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(audio_path.read_bytes())
        body.extend(b"\r\n--" + marker + b"--\r\n")
        return bytes(body)


class OpenAIWhisperSTT:
    """Governed Korean transcription plus English translation using whisper-1."""

    name = "openai-whisper-1"
    official = True
    provider = "openai"
    destination_host = "api.openai.com"
    model = "whisper-1"
    default_secret_ref = "keychain://ReelBrain/openai"
    maximum_file_bytes = OpenAIWhisperHTTPTransport.maximum_file_bytes

    def __init__(
        self,
        transport: WhisperTransport | None = None,
        *,
        speech_detector: SpeechWindowDetector | None = None,
        audio_extractor: AudioChunkExtractor | None = None,
        chunk_seconds: float = 600.0,
        overlap_seconds: float = 0.0,
    ) -> None:
        if chunk_seconds <= 0:
            raise ValueError("chunk_seconds_must_be_positive")
        if overlap_seconds != 0:
            raise ValueError("segment_timestamps_require_non_overlapping_audio_chunks")
        self.transport = transport or OpenAIWhisperHTTPTransport()
        self.speech_detector = speech_detector or FFmpegSpeechWindowDetector()
        self.audio_extractor = audio_extractor or FFmpegAudioChunkExtractor()
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds

    def transcribe_bilingual(
        self,
        video_path: Path | str,
        *,
        guard: RuntimeGuard,
        provider_consent_receipt: Mapping[str, object],
        budget_reservation_receipt: Mapping[str, object],
        secret_resolver: Callable[[str], str],
        secret_ref: str = default_secret_ref,
        secret_store_id: str = "reelbrain-keychain",
        secret_store_kind: str = "macos_keychain",
        secret_store_source: str = "ReelBrain/openai",
        checkpoint_dir: Path | str | None = None,
        checkpoint_scope: str = "",
    ) -> BilingualTranscript:
        source = Path(video_path).expanduser().resolve()
        guard.authorize_path(source, operation="read", data_class="source_media")
        windows = tuple(self.speech_detector.detect(source, guard))
        self._validate_windows(windows)
        guard.authorize_path(
            guard.workspace_root,
            operation="write",
            data_class="provider_audio_workspace",
        )
        with tempfile.TemporaryDirectory(
            prefix="reelbrain-openai-stt-", dir=guard.workspace_root
        ) as temp_name:
            chunks = self._extract_chunks(source, windows, Path(temp_name), guard)
            checkpoint_root = (
                Path(checkpoint_dir).expanduser().resolve()
                if checkpoint_dir is not None
                else None
            )
            if checkpoint_root is not None:
                if not checkpoint_scope.strip():
                    raise ValueError("provider_checkpoint_scope_required")
                guard.authorize_path(
                    checkpoint_root,
                    operation="write",
                    data_class="provider_checkpoint_directory",
                )
                checkpoint_root.mkdir(parents=True, exist_ok=True)

            def dispatch(api_key: str) -> BilingualTranscript:
                korean: list[TranscriptChunk] = []
                english: list[TranscriptChunk] = []
                for index, (start, end, audio_path) in enumerate(chunks, start=1):
                    if audio_path.stat().st_size > self.maximum_file_bytes:
                        raise MediaError("openai_audio_file_exceeds_25mb")
                    base_scope = {
                        "checkpoint_scope": checkpoint_scope,
                        "chunk_index": index,
                        "source_start": start,
                        "source_end": end,
                        "audio_sha256": self._path_sha256(audio_path),
                        "model": self.model,
                    }
                    original = self._checkpointed_response(
                        checkpoint_root,
                        f"chunk-{index:04d}-ko.json",
                        {**base_scope, "operation": "transcription", "language": "ko"},
                        lambda: self.transport.transcribe(
                            api_key=api_key,
                            audio_path=audio_path,
                            model=self.model,
                            language="ko",
                        ),
                        guard,
                    )
                    translated = self._checkpointed_response(
                        checkpoint_root,
                        f"chunk-{index:04d}-en.json",
                        {**base_scope, "operation": "translation", "language": "en"},
                        lambda: self.transport.translate(
                            api_key=api_key,
                            audio_path=audio_path,
                            model=self.model,
                        ),
                        guard,
                    )
                    korean_batch = self._response_chunks(
                        original, start, end - start, f"ko-{index}"
                    )
                    english_batch = self._response_chunks(
                        translated, start, end - start, f"en-{index}"
                    )
                    korean.extend(korean_batch)
                    english.extend(english_batch)
                aligned_korean, aligned_english = self._align_bilingual_chunks(
                    self._deduplicate_boundaries(korean),
                    self._deduplicate_boundaries(english),
                )
                return BilingualTranscript(
                    korean=aligned_korean,
                    english=aligned_english,
                    speech_windows=windows,
                )

            return guard.run_callback_tool(
                tool_id=self.name,
                capability="stt:transcribe",
                dispatch=dispatch,
                official=self.official,
                provider=self.provider,
                consent_receipt=provider_consent_receipt,
                destination_host=self.destination_host,
                budget_reservation_receipt=budget_reservation_receipt,
                secret_ref=secret_ref,
                secret_store_id=secret_store_id,
                secret_store_kind=secret_store_kind,
                secret_store_source=secret_store_source,
                secret_resolver=secret_resolver,
                failure_budget_state="partially_consumed",
                tool_description=(
                    "Transcribe creator-approved Korean speech audio with whisper-1 "
                    "and translate it to English while preserving source timestamps."
                ),
                input_schema={
                    "type": "object",
                    "required": ["audio_chunks", "language", "model"],
                },
                data_effects=(
                    "uploads bounded speech-only audio chunks to api.openai.com",
                    "writes bilingual transcript checkpoints and final local transcript",
                ),
            )

    @staticmethod
    def _path_sha256(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _checkpointed_response(
        root: Path | None,
        filename: str,
        scope: dict[str, object],
        invoke,
        guard: RuntimeGuard,
    ) -> Mapping[str, object]:
        path = root / filename if root is not None else None
        if path is not None and path.is_file():
            guard.authorize_path(path, operation="read", data_class="provider_checkpoint")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                response = document["response"]
                response_digest = sha256(
                    json.dumps(response, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if document.get("scope") == scope and document.get(
                    "response_sha256"
                ) == response_digest and isinstance(response, dict):
                    return response
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                pass
        response = invoke()
        if not isinstance(response, Mapping):
            raise RuntimeError("provider_checkpoint_response_invalid")
        response_document = dict(response)
        if path is not None:
            guard.authorize_path(path, operation="write", data_class="provider_checkpoint")
            payload = {
                "scope": scope,
                "response": response_document,
                "response_sha256": sha256(
                    json.dumps(
                        response_document, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
            }
            temporary = path.with_suffix(path.suffix + ".tmp")
            guard.authorize_path(
                temporary, operation="write", data_class="provider_checkpoint_temporary"
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
        return response_document

    def _extract_chunks(
        self,
        source: Path,
        windows: tuple[SpeechWindow, ...],
        root: Path,
        guard: RuntimeGuard,
    ) -> tuple[tuple[float, float, Path], ...]:
        extracted: list[tuple[float, float, Path]] = []
        for window in windows:
            cursor = window.start
            while cursor < window.end:
                end = min(window.end, cursor + self.chunk_seconds)
                destination = root / f"chunk-{len(extracted) + 1:04d}.mp3"
                self.audio_extractor.extract(
                    source=source,
                    start=cursor,
                    end=end,
                    destination=destination,
                    guard=guard,
                )
                if not destination.is_file() or destination.stat().st_size == 0:
                    raise MediaError("audio_chunk_extraction_failed")
                extracted.append((cursor, end, destination))
                if math.isclose(end, window.end) or end >= window.end:
                    break
                cursor = end - self.overlap_seconds
        if not extracted:
            raise MediaError("source_contains_no_detectable_speech")
        return tuple(extracted)

    @staticmethod
    def _validate_windows(windows: tuple[SpeechWindow, ...]) -> None:
        if not windows:
            raise MediaError("source_contains_no_detectable_speech")
        previous_end = -1.0
        for window in windows:
            if window.start < 0 or window.end <= window.start or window.start < previous_end:
                raise MediaError("speech_window_invalid")
            previous_end = window.end

    @staticmethod
    def _response_chunks(
        response: Mapping[str, object], offset: float, duration: float, prefix: str
    ) -> tuple[TranscriptChunk, ...]:
        raw_segments = response.get("segments")
        if not isinstance(raw_segments, list):
            raise RuntimeError("openai_whisper_response_segments_missing")
        chunks: list[TranscriptChunk] = []
        for segment in raw_segments:
            if not isinstance(segment, Mapping):
                raise RuntimeError("openai_whisper_response_segment_invalid")
            text = str(segment.get("text") or "").strip()
            try:
                relative_start = float(segment["start"])
                relative_end = float(segment["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("openai_whisper_response_timestamp_invalid") from exc
            if not text:
                continue
            if relative_start < 0 or relative_end <= relative_start:
                raise RuntimeError("openai_whisper_response_timestamp_invalid")
            no_speech_probability = float(segment.get("no_speech_prob", 0.0) or 0.0)
            if relative_start >= duration + 0.25:
                # Whisper can emit a repeated low-confidence tail after the bounded
                # audio has ended.  It is not source-grounded caption material, so
                # discard only when Whisper itself strongly marks it as no-speech.
                # A confident out-of-bounds segment still fails closed.
                if no_speech_probability >= 0.5:
                    continue
                raise RuntimeError("openai_whisper_response_timestamp_out_of_bounds")
            relative_end = min(relative_end, duration)
            if relative_end <= relative_start:
                continue
            chunks.append(
                TranscriptChunk(
                    chunk_id=f"{prefix}-{len(chunks) + 1}",
                    start=offset + relative_start,
                    end=offset + relative_end,
                    text=text,
                    confidence=max(0.0, min(1.0, 1.0 - no_speech_probability)),
                )
            )
        return tuple(chunks)

    @staticmethod
    def _deduplicate_boundaries(
        chunks: Sequence[TranscriptChunk],
    ) -> tuple[TranscriptChunk, ...]:
        ordered = sorted(chunks, key=lambda item: (item.start, item.end, item.chunk_id))
        output: list[TranscriptChunk] = []
        for chunk in ordered:
            normalized = " ".join(chunk.text.casefold().split())
            duplicate_index = next(
                (
                    index
                    for index in range(len(output) - 1, -1, -1)
                    if " ".join(output[index].text.casefold().split()) == normalized
                    and output[index].start < chunk.end
                    and chunk.start < output[index].end
                ),
                None,
            )
            if duplicate_index is None:
                output.append(chunk)
                continue
            existing = output[duplicate_index]
            if chunk.confidence > existing.confidence:
                output[duplicate_index] = TranscriptChunk(
                    chunk_id=existing.chunk_id,
                    start=min(existing.start, chunk.start),
                    end=max(existing.end, chunk.end),
                    text=chunk.text,
                    confidence=chunk.confidence,
                )
            elif chunk.end > existing.end:
                output[duplicate_index] = TranscriptChunk(
                    chunk_id=existing.chunk_id,
                    start=min(existing.start, chunk.start),
                    end=chunk.end,
                    text=existing.text,
                    confidence=existing.confidence,
                )
        monotonic: list[TranscriptChunk] = []
        for chunk in sorted(output, key=lambda item: (item.start, item.end, item.chunk_id)):
            if monotonic and chunk.start < monotonic[-1].end:
                raise RuntimeError("openai_whisper_response_segments_overlap")
            monotonic.append(
                TranscriptChunk(
                    chunk_id=chunk.chunk_id,
                    start=chunk.start,
                    end=chunk.end,
                    text=chunk.text,
                    confidence=chunk.confidence,
                )
            )
        return tuple(monotonic)

    @staticmethod
    def _align_bilingual_chunks(
        korean: Sequence[TranscriptChunk],
        english: Sequence[TranscriptChunk],
        *,
        tolerance_seconds: float = 12.0,
    ) -> tuple[tuple[TranscriptChunk, ...], tuple[TranscriptChunk, ...]]:
        """Merge timestamp-overlap components onto one canonical source timeline."""

        if not korean or not english:
            raise RuntimeError("bilingual_alignment_requires_both_tracks")
        # Translation and transcription are independent Whisper decodes.  During
        # internal silence one track can hallucinate a low-confidence segment that
        # has no temporal counterpart in the other language. Such an orphan is not
        # bilingual evidence and must not become a caption. Discard only marginal
        # orphans at or below 0.6 confidence; a stronger orphan must attach within
        # the bounded timing-repair window or still fail closed.
        korean = tuple(
            chunk
            for chunk in korean
            if chunk.confidence > 0.6
            or any(
                other.start < chunk.end and chunk.start < other.end
                for other in english
            )
        )
        english = tuple(
            chunk
            for chunk in english
            if chunk.confidence > 0.6
            or any(
                other.start < chunk.end and chunk.start < other.end
                for other in korean
            )
        )
        if not korean or not english:
            raise RuntimeError("bilingual_alignment_requires_both_tracks")
        total = len(korean) + len(english)
        parent = list(range(total))
        cross_edges = [0] * total

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for ko_index, ko_chunk in enumerate(korean):
            for en_index, en_chunk in enumerate(english):
                if (
                    ko_chunk.start < en_chunk.end
                    and en_chunk.start < ko_chunk.end
                ):
                    en_node = len(korean) + en_index
                    union(ko_index, en_node)
                    cross_edges[ko_index] += 1
                    cross_edges[en_node] += 1
        for index, count in enumerate(tuple(cross_edges)):
            if count != 0:
                continue
            chunk = korean[index] if index < len(korean) else english[index - len(korean)]
            candidates = (
                tuple(enumerate(english, start=len(korean)))
                if index < len(korean)
                else tuple(enumerate(korean))
            )
            nearest_index, nearest_gap = min(
                (
                    (
                        candidate_index,
                        max(
                            candidate.start - chunk.end,
                            chunk.start - candidate.end,
                            0.0,
                        ),
                    )
                    for candidate_index, candidate in candidates
                ),
                key=lambda item: item[1],
            )
            if nearest_gap <= tolerance_seconds:
                union(index, nearest_index)
                cross_edges[index] += 1
                cross_edges[nearest_index] += 1
        if any(count == 0 for count in cross_edges):
            raise RuntimeError("bilingual_translation_segment_unaligned")

        components: dict[int, tuple[list[int], list[int]]] = {}
        for index in range(total):
            root = find(index)
            ko_indices, en_indices = components.setdefault(root, ([], []))
            if index < len(korean):
                ko_indices.append(index)
            else:
                en_indices.append(index - len(korean))
        ordered = sorted(
            components.values(),
            key=lambda component: korean[min(component[0])].start,
        )
        aligned_korean: list[TranscriptChunk] = []
        aligned_english: list[TranscriptChunk] = []
        for index, (ko_indices, en_indices) in enumerate(ordered, start=1):
            if not ko_indices or not en_indices:
                raise RuntimeError("bilingual_translation_component_incomplete")
            ko_group = tuple(korean[item] for item in sorted(ko_indices))
            en_group = tuple(english[item] for item in sorted(en_indices))
            start = ko_group[0].start
            end = ko_group[-1].end
            if aligned_korean and start < aligned_korean[-1].end:
                raise RuntimeError("bilingual_alignment_timeline_overlap")
            aligned_korean.append(
                TranscriptChunk(
                    chunk_id=f"aligned-ko-{index:05d}",
                    start=start,
                    end=end,
                    text=" ".join(chunk.text.strip() for chunk in ko_group),
                    confidence=min(chunk.confidence for chunk in ko_group),
                )
            )
            aligned_english.append(
                TranscriptChunk(
                    chunk_id=f"aligned-en-{index:05d}",
                    start=start,
                    end=end,
                    text=" ".join(chunk.text.strip() for chunk in en_group),
                    confidence=min(chunk.confidence for chunk in en_group),
                )
            )
        return tuple(aligned_korean), tuple(aligned_english)

class SubtitleFileSTT:
    """Use a creator-supplied SRT/VTT as a local transcription reference."""

    name = "subtitle-file-stt"
    official = True
    provider = None
    reference_kind = "creator_supplied_transcript"

    def __init__(self, transcript_path: Path | str) -> None:
        self.transcript_path = Path(transcript_path).expanduser().resolve()
        self.input_paths = (self.transcript_path,)

    def transcribe(self, video_path: Path) -> tuple[TranscriptChunk, ...]:
        if self.transcript_path.suffix.lower() not in {".srt", ".vtt"}:
            raise MediaError("unsupported_transcript_format")
        if not self.transcript_path.is_file():
            raise MediaError("transcript_file_missing")
        text = self.transcript_path.read_text(encoding="utf-8-sig")
        matcher = _VTT_TIME if self.transcript_path.suffix.lower() == ".vtt" else _SRT_TIME
        lines = text.splitlines()
        chunks: list[TranscriptChunk] = []
        index = 0
        while index < len(lines):
            match = matcher.search(lines[index])
            if match is None:
                index += 1
                continue
            start = _subtitle_seconds(match.group("start"))
            end = _subtitle_seconds(match.group("end"))
            index += 1
            cue_lines: list[str] = []
            while index < len(lines) and lines[index].strip():
                cue_lines.append(lines[index].strip())
                index += 1
            cue_text = " ".join(cue_lines).strip()
            if end <= start or not cue_text:
                raise MediaError("invalid_subtitle_cue")
            chunks.append(
                TranscriptChunk(
                    chunk_id=f"subtitle-{len(chunks) + 1}",
                    start=start,
                    end=end,
                    text=cue_text,
                    confidence=1.0,
                )
            )
            index += 1
        if not chunks:
            raise MediaError("transcript_contains_no_cues")
        return tuple(chunks)


class LocalWhisperSTT:
    """Local Whisper CLI adapter; no cloud fallback is performed."""

    name = "local-whisper"
    official = True
    provider = None

    def __init__(self, *, model: str = "base", language: str | None = None) -> None:
        self.model = model
        self.language = language

    def transcribe(self, video_path: Path) -> tuple[TranscriptChunk, ...]:
        if shutil.which("whisper") is None:
            raise MediaError("local_whisper_not_installed")
        with tempfile.TemporaryDirectory(prefix="reelbrain-stt-") as temp_name:
            output_dir = Path(temp_name)
            guard = RuntimeGuard(
                workspace_root=output_dir,
                local_allowlist=(video_path.parent,),
                project_id="local-whisper-stt",
                creator_id="local-creator",
                agent_id="meaning-scout",
                tool_names=("whisper",),
            )
            guard.authorize_path(video_path, operation="read", data_class="source_media")
            command = [
                "whisper",
                str(video_path),
                "--model",
                self.model,
                "--output_format",
                "json",
                "--output_dir",
                str(output_dir),
            ]
            if self.language:
                command.extend(["--language", self.language])
            run_command(command, guard)
            result_path = output_dir / f"{video_path.stem}.json"
            if not result_path.is_file():
                raise MediaError("whisper_transcript_artifact_missing")
            guard.authorize_path(result_path, operation="read", data_class="transcript")
            document = json.loads(result_path.read_text(encoding="utf-8"))
            return tuple(
                TranscriptChunk(
                    chunk_id=f"whisper-{index}",
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                    text=segment["text"].strip(),
                    confidence=max(0.0, min(1.0, 1.0 - float(segment.get("no_speech_prob", 0)))),
                )
                for index, segment in enumerate(document.get("segments", []), start=1)
                if segment.get("text", "").strip()
            )
