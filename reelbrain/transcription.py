"""Selectable STT adapters, including a local Whisper CLI default."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Protocol

from .editing import MediaError, run_command
from .runtime_guard import RuntimeGuard


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: str
    start: float
    end: float
    text: str
    confidence: float = 1.0


class STTProvider(Protocol):
    name: str

    def transcribe(self, video_path: Path) -> tuple[TranscriptChunk, ...]: ...


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
