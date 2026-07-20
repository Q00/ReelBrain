"""Selectable STT adapters, including a local Whisper CLI default."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
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


class LocalWhisperSTT:
    """Local Whisper CLI adapter; no cloud fallback is performed."""

    name = "local-whisper"

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
