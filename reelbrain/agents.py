"""Deterministic reference personas for highlight fan-out and showrunner synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .editing import TranscriptSegment


@dataclass(frozen=True)
class CandidateAssessment:
    persona: str
    segment_id: str
    score: float
    rationale: str
    risks: tuple[str, ...] = ()


class MeaningScout:
    name = "Meaning Scout"

    def assess(self, segment: TranscriptSegment) -> CandidateAssessment:
        score = 0.45 * segment.educational_value + 0.35 * segment.confidence
        score += 0.1 if segment.self_contained else -0.4
        score += 0.1 if segment.complete_thought else -0.4
        return CandidateAssessment(
            self.name,
            segment.segment_id,
            score,
            "scores educational value, confidence, self-containment, and complete thought",
            segment.risks,
        )


class HookScout:
    name = "Hook Scout"

    def assess(self, segment: TranscriptSegment) -> CandidateAssessment:
        hook_signal = min(len(segment.hook.split()) / 12, 1.0)
        payoff_signal = min(len(segment.payoff.split()) / 12, 1.0)
        return CandidateAssessment(
            self.name,
            segment.segment_id,
            0.5 * hook_signal + 0.5 * payoff_signal,
            "scores an understandable hook with an explicit payoff",
        )


class CreatorAdvocate:
    name = "Creator Advocate"

    def __init__(self, preferred_terms: Iterable[str] = ()) -> None:
        self.preferred_terms = tuple(term.lower() for term in preferred_terms)

    def assess(self, segment: TranscriptSegment) -> CandidateAssessment:
        text = f"{segment.text} {segment.thesis} {segment.takeaway}".lower()
        matches = sum(term in text for term in self.preferred_terms)
        score = 0.6 + min(matches * 0.1, 0.4)
        return CandidateAssessment(
            self.name,
            segment.segment_id,
            score,
            "checks the candidate against explicit creator language preferences",
        )


class ContextGuardian:
    name = "Context Guardian"

    def assess(self, segment: TranscriptSegment) -> CandidateAssessment:
        risks = list(segment.risks)
        if segment.required_context:
            risks.append("requires_prior_context")
        if not segment.complete_thought:
            risks.append("cuts_mid_thought")
        score = 1.0 - min(0.25 * len(risks), 1.0)
        return CandidateAssessment(
            self.name,
            segment.segment_id,
            score,
            "penalizes missing context, semantic risk, and incomplete thoughts",
            tuple(risks),
        )


class Showrunner:
    name = "Showrunner"

    def synthesize(
        self,
        segments: Iterable[TranscriptSegment],
        assessments: Iterable[CandidateAssessment],
        *,
        count: int = 3,
    ) -> tuple[TranscriptSegment, ...]:
        candidates = tuple(segments)
        by_segment: dict[str, list[CandidateAssessment]] = {}
        for assessment in assessments:
            by_segment.setdefault(assessment.segment_id, []).append(assessment)
        ranked = sorted(
            candidates,
            key=lambda segment: (
                min((item.score for item in by_segment.get(segment.segment_id, ())), default=0),
                sum(item.score for item in by_segment.get(segment.segment_id, ())),
                segment.educational_value,
            ),
            reverse=True,
        )
        selected: list[TranscriptSegment] = []
        for candidate in ranked:
            if any(candidate.takeaway == item.takeaway for item in selected):
                continue
            if any(
                max(0.0, min(candidate.end, other.end) - max(candidate.start, other.start))
                / min(candidate.duration, other.duration)
                > 0.2
                for other in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) == count:
                break
        if len(selected) < count:
            raise ValueError("showrunner_insufficient_pareto_candidates")
        return tuple(selected)


class HighlightAgentTeam:
    """Fan-out maximizes recall; the Showrunner returns a diverse Pareto set."""

    def __init__(self, *, preferred_terms: Iterable[str] = ()) -> None:
        self.personas = (
            MeaningScout(),
            HookScout(),
            CreatorAdvocate(preferred_terms),
            ContextGuardian(),
        )
        self.showrunner = Showrunner()

    def select(
        self, segments: Iterable[TranscriptSegment], *, count: int = 3
    ) -> tuple[tuple[TranscriptSegment, ...], tuple[CandidateAssessment, ...]]:
        candidates = tuple(segments)
        assessments = tuple(
            persona.assess(candidate)
            for candidate in candidates
            for persona in self.personas
        )
        return self.showrunner.synthesize(candidates, assessments, count=count), assessments

