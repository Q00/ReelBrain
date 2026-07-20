"""Creator-confirmation-gated long-form planning from local transcripts."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
from typing import Iterable

from .agents import HighlightAgentTeam
from .editing import MediaError, TranscriptSegment, file_digest, probe_media
from .runtime_guard import RuntimeGuard


def long_form_windows(chunks: Iterable[object]) -> tuple[TranscriptSegment, ...]:
    """Group timestamped transcript cues into source-grounded 60-120s arguments."""

    ordered = tuple(sorted(chunks, key=lambda chunk: chunk.start))
    windows: list[TranscriptSegment] = []
    current: list[object] = []
    for chunk in ordered:
        if current and chunk.start - current[-1].end > 5:
            current = []
        current.append(chunk)
        duration = current[-1].end - current[0].start
        if duration < 60:
            continue
        text = " ".join(item.text.strip() for item in current).strip()
        if duration < 120 and not text.endswith((".", "!", "?", "。", "다.")):
            continue
        sentences = [part.strip() for part in text.replace("?", ".").split(".") if part.strip()]
        thesis = sentences[0] if sentences else text[:120]
        payoff = sentences[-1] if sentences else text[-120:]
        confidence = sum(item.confidence for item in current) / len(current)
        windows.append(
            TranscriptSegment(
                segment_id=f"long-window-{current[0].chunk_id}-{current[-1].chunk_id}",
                start=current[0].start,
                end=current[-1].end,
                text=text,
                thesis=thesis,
                takeaway=text,
                hook=" ".join(text.split()[:12]),
                payoff=payoff,
                confidence=confidence,
                educational_value=confidence,
                self_contained=True,
                complete_thought=True,
                must_keep=False,
            )
        )
        current = []
    return tuple(windows)


class LongFormPlanBuilder:
    """Propose, but never approve, a long-form argument map."""

    def propose(
        self,
        *,
        source: Path | str,
        transcript_provider,
        output_dir: Path | str,
        project_id: str,
        creator_id: str,
        preferred_terms: Iterable[str] = (),
    ) -> dict[str, Path]:
        source_path = Path(source).expanduser().resolve()
        root = Path(output_dir).expanduser().resolve()
        provider_inputs = tuple(
            Path(path).expanduser().resolve()
            for path in getattr(transcript_provider, "input_paths", ())
        )
        guard = RuntimeGuard(
            workspace_root=root,
            local_allowlist=(source_path.parent, *(path.parent for path in provider_inputs)),
            project_id=project_id,
            creator_id=creator_id,
            agent_id="showrunner",
            tool_names=("ffprobe",),
        )
        guard.authorize_path(source_path, operation="read", data_class="source_media")
        for path in provider_inputs:
            guard.authorize_path(path, operation="read", data_class="transcript_reference")
        info = probe_media(source_path, guard)
        if not 20 * 60 <= info.duration_seconds <= 60 * 60:
            raise MediaError("long_form_source_duration_must_be_20_to_60_minutes")
        chunks = tuple(
            guard.run_callback_tool(
                tool_id=transcript_provider.name,
                capability="stt:transcribe",
                dispatch=lambda: transcript_provider.transcribe(source_path),
                official=bool(getattr(transcript_provider, "official", False)),
                provider=getattr(transcript_provider, "provider", None),
            )
        )
        candidates = long_form_windows(chunks)
        if not candidates:
            raise MediaError("no_long_form_argument_candidates")
        team = HighlightAgentTeam(preferred_terms=preferred_terms)
        assessments = tuple(
            persona.assess(candidate)
            for candidate in candidates
            for persona in team.personas
        )
        score_by_segment: dict[str, float] = {}
        for assessment in assessments:
            score_by_segment[assessment.segment_id] = (
                score_by_segment.get(assessment.segment_id, 0.0) + assessment.score
            )
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                score_by_segment.get(candidate.segment_id, 0.0),
                candidate.educational_value,
            ),
            reverse=True,
        )
        selected: list[TranscriptSegment] = []
        duration = 0.0
        for candidate in ranked:
            if duration + candidate.duration > 720:
                continue
            selected.append(candidate)
            duration += candidate.duration
            if duration >= 300:
                break
        if duration < 300:
            raise MediaError("insufficient_long_form_argument_duration")
        selected.sort(key=lambda candidate: candidate.start)
        linked = tuple(
            replace(
                candidate,
                required_context=(selected[index - 1].segment_id,) if index else (),
            )
            for index, candidate in enumerate(selected)
        )
        root.mkdir(parents=True, exist_ok=True)
        argument_map = root / "proposed_argument_map.json"
        assessments_path = root / "agent_assessments.json"
        report = root / "long_plan_report.json"
        guard.authorize_path(argument_map, operation="write", data_class="argument_map")
        guard.authorize_path(assessments_path, operation="write", data_class="agent_assessments")
        guard.authorize_path(report, operation="write", data_class="planning_report")
        argument_map.write_text(
            json.dumps([asdict(segment) for segment in linked], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        assessments_path.write_text(
            json.dumps([asdict(item) for item in assessments], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        report.write_text(
            json.dumps(
                {
                    "status": "CREATOR_CONFIRMATION_REQUIRED",
                    "project_id": project_id,
                    "creator_id": creator_id,
                    "source": str(source_path),
                    "source_digest": file_digest(source_path, guard),
                    "selected_duration_seconds": duration,
                    "selected_segment_ids": [segment.segment_id for segment in linked],
                    "creator_confirmed": False,
                    "publish_ready": False,
                    "next_action": "Review/edit the proposed argument map, then pass the confirmed file to reelbrain long.",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return {
            "argument_map": argument_map,
            "agent_assessments": assessments_path,
            "report": report,
        }
