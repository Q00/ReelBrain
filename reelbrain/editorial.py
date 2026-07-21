"""Grounded editorial fan-out planning with provider-governed dispatch.

The module deliberately separates editorial judgment from source-of-truth
validation.  Agents may rank and explain only IDs from deterministic transcript
window catalogs; timestamps and sentence boundaries remain host-validated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from typing import Callable, Iterable, Literal, Mapping, Protocol
from urllib.request import Request, urlopen

from .runtime_guard import RuntimeGuard


_SENTENCE_END = re.compile(r"[.!?。！？][\"'’”)\]]*$")
_PERSONAS = (
    "Story Editor",
    "Retention Editor",
    "Style Editor",
    "Continuity Editor",
)


class EditorialValidationError(ValueError):
    """Raised when an editorial response is not grounded in the transcript."""


@dataclass(frozen=True)
class ProviderMetadata:
    """RuntimeGuard-compatible identity for an editorial transport."""

    tool_id: str
    capability: str
    provider: str | None
    destination_host: str | None
    official: bool
    model: str


@dataclass(frozen=True)
class TranscriptWindow:
    window_id: str
    kind: Literal["short", "long_form"]
    chunk_ids: tuple[str, ...]
    start_seconds: float
    end_seconds: float
    text: str
    confidence: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def start_chunk_id(self) -> str:
        return self.chunk_ids[0]

    @property
    def end_chunk_id(self) -> str:
        return self.chunk_ids[-1]


@dataclass(frozen=True)
class PersonaCandidateSelection:
    candidate_id: str
    start_chunk_id: str
    end_chunk_id: str
    start_seconds: float
    end_seconds: float
    score: float
    rationale: str
    risks: tuple[str, ...]


@dataclass(frozen=True)
class PersonaLaneResult:
    persona: str
    selections: tuple[PersonaCandidateSelection, ...]


@dataclass(frozen=True)
class ShortDraft:
    candidate_id: str
    chunk_ids: tuple[str, ...]
    start_seconds: float
    end_seconds: float
    text: str
    angle: str
    rationale: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class LongFormSection:
    section_id: str
    title: str
    chunk_ids: tuple[str, ...]
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class LongFormDraftPlan:
    window_id: str
    chunk_ids: tuple[str, ...]
    start_seconds: float
    end_seconds: float
    title: str
    thesis: str
    rationale: str
    sections: tuple[LongFormSection, ...]

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class EditorialTraceRecord:
    sequence: int
    stage: Literal["persona_assessment", "showrunner_synthesis"]
    agent: str
    system_prompt: str
    tool_description: str
    response_schema: dict[str, object]
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    validation: str
    selection_rationale: tuple[str, ...]
    provider_metadata: dict[str, object]


@dataclass(frozen=True)
class EditorialPlan:
    shorts: tuple[ShortDraft, ...]
    long_form: LongFormDraftPlan
    persona_selections: tuple[PersonaLaneResult, ...]
    trace: tuple[EditorialTraceRecord, ...]


class EditorialTransport(Protocol):
    metadata: ProviderMetadata

    def respond(
        self,
        *,
        system_prompt: str,
        input_payload: dict[str, object],
        json_schema: dict[str, object],
        schema_name: str,
        api_key: str | None = None,
    ) -> dict[str, object]: ...


class OpenAIResponsesHTTPTransport:
    """Minimal OpenAI Responses transport using strict structured output."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        model: str = "gpt-5.4-mini",
        opener: Callable[..., object] | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.model = model
        self.metadata = ProviderMetadata(
            tool_id="openai-responses-editorial",
            capability="editorial:plan",
            provider="openai",
            destination_host="api.openai.com",
            official=True,
            model=model,
        )
        self._opener = opener or urlopen
        self.timeout_seconds = timeout_seconds

    def respond(
        self,
        *,
        system_prompt: str,
        input_payload: dict[str, object],
        json_schema: dict[str, object],
        schema_name: str,
        api_key: str | None = None,
    ) -> dict[str, object]:
        if not api_key or not api_key.strip():
            raise PermissionError("openai_api_key_required_inside_dispatch")
        body = {
            "model": self.model,
            "store": False,
            "instructions": system_prompt,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                input_payload, ensure_ascii=False, sort_keys=True
                            ),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                }
            },
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self._opener(request, timeout=self.timeout_seconds) as response:
            document = json.loads(response.read().decode("utf-8"))
        output_text = document.get("output_text")
        if not isinstance(output_text, str):
            output_text = _responses_output_text(document)
        if not output_text:
            raise RuntimeError("openai_editorial_response_missing_output_text")
        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            raise RuntimeError("openai_editorial_response_must_be_json_object")
        return parsed


@dataclass(frozen=True)
class _SourceChunk:
    chunk_id: str
    start: float
    end: float
    text: str
    confidence: float


def _responses_output_text(document: Mapping[str, object]) -> str | None:
    for output in document.get("output", ()):
        if not isinstance(output, Mapping):
            continue
        for content in output.get("content", ()):
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if content.get("type") == "output_text" and isinstance(text, str):
                return text
    return None


def _coerce_chunks(chunks: Iterable[object]) -> tuple[_SourceChunk, ...]:
    normalized: list[_SourceChunk] = []
    seen_ids: set[str] = set()
    for value in chunks:
        try:
            chunk_id = str(getattr(value, "chunk_id")).strip()
            start = float(getattr(value, "start"))
            end = float(getattr(value, "end"))
            text = str(getattr(value, "text")).strip()
            confidence = float(getattr(value, "confidence", 1.0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise EditorialValidationError("invalid_timestamped_transcript_chunk") from exc
        if not chunk_id or chunk_id in seen_ids:
            raise EditorialValidationError("transcript_chunk_ids_must_be_unique")
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise EditorialValidationError("invalid_transcript_chunk_timestamp")
        if not text:
            raise EditorialValidationError("transcript_chunk_text_required")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise EditorialValidationError("transcript_chunk_confidence_out_of_range")
        seen_ids.add(chunk_id)
        normalized.append(_SourceChunk(chunk_id, start, end, text, confidence))
    normalized.sort(key=lambda item: (item.start, item.end, item.chunk_id))
    if not normalized:
        raise EditorialValidationError("timestamped_transcript_required")
    for previous, current in zip(normalized, normalized[1:]):
        if current.start < previous.end:
            raise EditorialValidationError("transcript_chunks_must_not_overlap")
    return tuple(normalized)


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip()))


def _natural_start(chunks: tuple[_SourceChunk, ...], index: int) -> bool:
    return index == 0 or _ends_sentence(chunks[index - 1].text)


def _distributed_subset(
    spans: list[tuple[int, int]], max_windows: int | None
) -> list[tuple[int, int]]:
    if max_windows is None or len(spans) <= max_windows:
        return spans
    if max_windows < 1:
        raise EditorialValidationError("max_windows_must_be_positive")
    if max_windows == 1:
        return [spans[len(spans) // 2]]
    indexes = {
        round(position * (len(spans) - 1) / (max_windows - 1))
        for position in range(max_windows)
    }
    return [spans[index] for index in sorted(indexes)]


def _window_id(kind: str, chunks: tuple[_SourceChunk, ...]) -> str:
    identity = json.dumps(
        [kind, chunks[0].chunk_id, chunks[-1].chunk_id, chunks[0].start, chunks[-1].end],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{kind}-{sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _build_windows(
    chunks: tuple[_SourceChunk, ...],
    *,
    kind: Literal["short", "long_form"],
    min_seconds: float,
    max_seconds: float,
    max_gap_seconds: float,
    max_windows: int | None,
) -> tuple[TranscriptWindow, ...]:
    if min_seconds <= 0 or max_seconds < min_seconds:
        raise EditorialValidationError("invalid_window_duration_limits")
    if max_gap_seconds < 0:
        raise EditorialValidationError("max_gap_seconds_must_be_non_negative")
    spans: list[tuple[int, int]] = []
    for start_index, first in enumerate(chunks):
        if not _natural_start(chunks, start_index):
            continue
        for end_index in range(start_index, len(chunks)):
            current = chunks[end_index]
            if end_index > start_index:
                previous = chunks[end_index - 1]
                if current.start - previous.end > max_gap_seconds:
                    break
            duration = current.end - first.start
            if duration > max_seconds:
                break
            if duration >= min_seconds and _ends_sentence(current.text):
                spans.append((start_index, end_index))
    spans = _distributed_subset(spans, max_windows)
    windows: list[TranscriptWindow] = []
    for start_index, end_index in spans:
        source = chunks[start_index : end_index + 1]
        windows.append(
            TranscriptWindow(
                window_id=_window_id(kind, source),
                kind=kind,
                chunk_ids=tuple(item.chunk_id for item in source),
                start_seconds=source[0].start,
                end_seconds=source[-1].end,
                text=" ".join(item.text for item in source),
                confidence=sum(item.confidence for item in source) / len(source),
            )
        )
    return tuple(windows)


def build_short_candidate_windows(
    chunks: Iterable[object],
    *,
    min_seconds: float = 30.0,
    max_seconds: float = 60.0,
    max_gap_seconds: float = 5.0,
    max_windows: int | None = 240,
) -> tuple[TranscriptWindow, ...]:
    """Build source-grounded short candidates ending on sentence boundaries."""

    return _build_windows(
        _coerce_chunks(chunks),
        kind="short",
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        max_gap_seconds=max_gap_seconds,
        max_windows=max_windows,
    )


def build_long_form_candidate_windows(
    chunks: Iterable[object],
    *,
    min_seconds: float = 600.0,
    max_seconds: float = 900.0,
    max_gap_seconds: float = 5.0,
    max_windows: int | None = 24,
) -> tuple[TranscriptWindow, ...]:
    """Build contiguous 10-15 minute long-form draft options."""

    return _build_windows(
        _coerce_chunks(chunks),
        kind="long_form",
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        max_gap_seconds=max_gap_seconds,
        max_windows=max_windows,
    )


def _maximum_non_overlapping_window_count(
    windows: Iterable[TranscriptWindow],
) -> int:
    selected = 0
    last_end = -math.inf
    for window in sorted(windows, key=lambda item: (item.end_seconds, item.start_seconds)):
        if window.start_seconds >= last_end:
            selected += 1
            last_end = window.end_seconds
    return selected


_PERSONA_PROMPTS = {
    "Story Editor": (
        "Build a self-contained educational arc with a clear setup, explanation, and "
        "payoff. Prefer natural edit boundaries and complete thoughts."
    ),
    "Retention Editor": (
        "Find openings, pacing opportunities, and concrete payoffs that hold attention. "
        "Do not reward sensationalism unsupported by the transcript."
    ),
    "Style Editor": (
        "Apply the creator's stated voice, caption rhythm, framing preferences, visual "
        "emphasis, and brand. Treat preference memory as a behavior prior, never evidence."
    ),
    "Continuity Editor": (
        "Reject context-dependent excerpts, semantic distortion, missing caveats, and "
        "cuts that would end mid-thought."
    ),
}

_PERSONA_TOOL_DESCRIPTION = (
    "editorial_candidate_selector (read-only): select only candidate IDs present in "
    "the supplied catalog; echo exact chunk boundary IDs and timestamps; give a "
    "0-1 score, rationale, and explicit risks. Never invent transcript evidence."
)

def _showrunner_prompt(short_count: int) -> str:
    return (
        "You are ReelBrain's Showrunner. Synthesize the four independent editorial lanes. "
        f"Choose exactly {short_count} non-overlapping shorts with distinct editorial "
        "angles and one coherent 10-15 minute long-form option. Use only catalog IDs "
        "and exact source boundaries. Creator outcomes determine quality; tool-sequence "
        "conformity is not a quality proxy."
    )

_SHOWRUNNER_TOOL_DESCRIPTION = (
    "editorial_plan_selector (read-only): choose grounded short candidate IDs and one "
    "grounded long-form window ID. Echo exact source IDs/timestamps and explain the "
    "cross-lane selection rationale. It cannot render, publish, or alter the transcript."
)


def _closed_object(
    properties: dict[str, object], required: Iterable[str]
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_REFERENCE_PROPERTIES: dict[str, object] = {
    "candidate_id": {"type": "string"},
    "start_chunk_id": {"type": "string"},
    "end_chunk_id": {"type": "string"},
    "start_seconds": {"type": "number"},
    "end_seconds": {"type": "number"},
}

_PERSONA_SCHEMA = _closed_object(
    {
        "selections": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": _closed_object(
                {
                    **_REFERENCE_PROPERTIES,
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string", "minLength": 1},
                    "risks": {"type": "array", "items": {"type": "string"}},
                },
                (
                    *tuple(_REFERENCE_PROPERTIES),
                    "score",
                    "rationale",
                    "risks",
                ),
            ),
        }
    },
    ("selections",),
)

_SHOWRUNNER_SHORT_PROPERTIES = {
    **_REFERENCE_PROPERTIES,
    "angle": {"type": "string", "minLength": 1},
    "rationale": {"type": "string", "minLength": 1},
}

_SHOWRUNNER_SCHEMA = _closed_object(
    {
        "shorts": {
            "type": "array",
            "minItems": 2,
            "maxItems": 10,
            "items": _closed_object(
                _SHOWRUNNER_SHORT_PROPERTIES,
                tuple(_SHOWRUNNER_SHORT_PROPERTIES),
            ),
        },
        "long_form": _closed_object(
            {
                "window_id": {"type": "string"},
                "start_chunk_id": {"type": "string"},
                "end_chunk_id": {"type": "string"},
                "start_seconds": {"type": "number"},
                "end_seconds": {"type": "number"},
                "title": {"type": "string", "minLength": 1},
                "thesis": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
            },
            (
                "window_id",
                "start_chunk_id",
                "end_chunk_id",
                "start_seconds",
                "end_seconds",
                "title",
                "thesis",
                "rationale",
            ),
        ),
        "selection_rationale": {"type": "string", "minLength": 1},
    },
    ("shorts", "long_form", "selection_rationale"),
)


def _showrunner_schema(short_count: int) -> dict[str, object]:
    schema = json.loads(json.dumps(_SHOWRUNNER_SCHEMA))
    shorts = schema["properties"]["shorts"]
    shorts["minItems"] = short_count
    shorts["maxItems"] = short_count
    return schema


def _window_payload(window: TranscriptWindow, *, include_text: bool = True) -> dict[str, object]:
    words = window.text.split()
    payload: dict[str, object] = {
        "candidate_id" if window.kind == "short" else "window_id": window.window_id,
        "start_chunk_id": window.start_chunk_id,
        "end_chunk_id": window.end_chunk_id,
        "chunk_ids": list(window.chunk_ids),
        "start_seconds": window.start_seconds,
        "end_seconds": window.end_seconds,
        "duration_seconds": window.duration_seconds,
        "confidence": window.confidence,
    }
    if include_text:
        payload["text"] = window.text
    else:
        payload["opening_excerpt"] = " ".join(words[:60])
        payload["closing_excerpt"] = " ".join(words[-60:])
    return payload


def _source_payload(chunks: tuple[_SourceChunk, ...]) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": item.chunk_id,
            "start_seconds": item.start,
            "end_seconds": item.end,
            "text": item.text,
            "confidence": item.confidence,
        }
        for item in chunks
    ]


def _exact_keys(value: Mapping[str, object], expected: set[str], error: str) -> None:
    if set(value) != expected:
        raise EditorialValidationError(error)


def _as_mapping(value: object, error: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EditorialValidationError(error)
    return value


def _as_nonempty_string(value: object, error: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditorialValidationError(error)
    return value.strip()


def _as_number(value: object, error: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EditorialValidationError(error)
    result = float(value)
    if not math.isfinite(result):
        raise EditorialValidationError(error)
    return result


def _validate_reference(
    value: Mapping[str, object],
    *,
    windows: Mapping[str, TranscriptWindow],
    chunks: tuple[_SourceChunk, ...],
    id_key: str = "candidate_id",
) -> TranscriptWindow:
    reference_id = _as_nonempty_string(value.get(id_key), "editorial_id_required")
    if reference_id not in windows:
        raise EditorialValidationError("invented_editorial_id")
    start_chunk_id = _as_nonempty_string(
        value.get("start_chunk_id"), "source_boundary_id_required"
    )
    end_chunk_id = _as_nonempty_string(
        value.get("end_chunk_id"), "source_boundary_id_required"
    )
    by_id = {item.chunk_id: item for item in chunks}
    indexes = {item.chunk_id: index for index, item in enumerate(chunks)}
    if start_chunk_id not in by_id or end_chunk_id not in by_id:
        raise EditorialValidationError("invented_transcript_chunk_id")
    window = windows[reference_id]
    _as_number(
        value.get("start_seconds"), "editorial_timestamp_required"
    )
    _as_number(value.get("end_seconds"), "editorial_timestamp_required")
    # The content-addressed candidate ID is the capability reference.  Repeated
    # chunk IDs and timestamps in a model response are inspectable claims, not
    # authority to mutate the window.  Resolve the immutable source-grounded
    # boundaries from the ID so an agent cannot stretch or shift a valid choice.
    if not _natural_start(chunks, indexes[window.start_chunk_id]):
        raise EditorialValidationError("mid_thought_start_boundary")
    if not _ends_sentence(by_id[window.end_chunk_id].text):
        raise EditorialValidationError("mid_thought_endpoint")
    return window


def _persona_response(
    persona: str,
    response: Mapping[str, object],
    *,
    windows: Mapping[str, TranscriptWindow],
    chunks: tuple[_SourceChunk, ...],
) -> PersonaLaneResult:
    _exact_keys(response, {"selections"}, "persona_response_schema_violation")
    selections_value = response.get("selections")
    if not isinstance(selections_value, list) or not 1 <= len(selections_value) <= 20:
        raise EditorialValidationError("persona_selection_count_invalid")
    expected_keys = {
        "candidate_id",
        "start_chunk_id",
        "end_chunk_id",
        "start_seconds",
        "end_seconds",
        "score",
        "rationale",
        "risks",
    }
    selections: list[PersonaCandidateSelection] = []
    seen: set[str] = set()
    for raw in selections_value:
        item = _as_mapping(raw, "persona_selection_schema_violation")
        _exact_keys(item, expected_keys, "persona_selection_schema_violation")
        candidate_id = _as_nonempty_string(
            item.get("candidate_id"), "editorial_id_required"
        )
        if candidate_id not in windows:
            # A persona is advisory fan-out, not the final authority. Preserve
            # the raw response in the trace but discard an invented reference
            # when the same lane still supplies grounded candidates. The
            # Showrunner remains strict and cannot select an unknown ID.
            continue
        window = _validate_reference(item, windows=windows, chunks=chunks)
        if window.window_id in seen:
            raise EditorialValidationError("persona_duplicate_candidate_id")
        seen.add(window.window_id)
        score = _as_number(item.get("score"), "persona_score_invalid")
        if not 0 <= score <= 1:
            raise EditorialValidationError("persona_score_invalid")
        rationale = _as_nonempty_string(
            item.get("rationale"), "persona_rationale_required"
        )
        risks_value = item.get("risks")
        if not isinstance(risks_value, list) or not all(
            isinstance(risk, str) and risk.strip() for risk in risks_value
        ):
            raise EditorialValidationError("persona_risks_invalid")
        selections.append(
            PersonaCandidateSelection(
                candidate_id=window.window_id,
                start_chunk_id=window.start_chunk_id,
                end_chunk_id=window.end_chunk_id,
                start_seconds=window.start_seconds,
                end_seconds=window.end_seconds,
                score=score,
                rationale=rationale,
                risks=tuple(risk.strip() for risk in risks_value),
            )
        )
    if not selections:
        raise EditorialValidationError("persona_requires_grounded_selection")
    return PersonaLaneResult(persona, tuple(selections))


def _make_sections(
    window: TranscriptWindow,
    chunks: tuple[_SourceChunk, ...],
) -> tuple[LongFormSection, ...]:
    by_id = {item.chunk_id: item for item in chunks}
    selected = tuple(by_id[chunk_id] for chunk_id in window.chunk_ids)
    if len(selected) == 1:
        boundary_indexes: list[int] = []
    else:
        natural_indexes = [
            index
            for index, chunk in enumerate(selected[:-1])
            if _ends_sentence(chunk.text)
        ]
        desired = [
            window.start_seconds + window.duration_seconds * fraction
            for fraction in (0.25, 0.5, 0.75)
        ]
        boundary_indexes = []
        floor = -1
        for target in desired:
            options = [index for index in natural_indexes if index > floor]
            if not options:
                break
            chosen = min(options, key=lambda index: abs(selected[index].end - target))
            if chosen >= len(selected) - 1:
                break
            boundary_indexes.append(chosen)
            floor = chosen
        boundary_indexes = sorted(set(boundary_indexes))
    ranges: list[tuple[int, int]] = []
    start = 0
    for boundary in boundary_indexes:
        if boundary >= start:
            ranges.append((start, boundary))
            start = boundary + 1
    ranges.append((start, len(selected) - 1))
    sections: list[LongFormSection] = []
    for index, (start_index, end_index) in enumerate(ranges, start=1):
        section_chunks = selected[start_index : end_index + 1]
        first_words = " ".join(section_chunks[0].text.split()[:10]).rstrip(".!?")
        sections.append(
            LongFormSection(
                section_id=f"{window.window_id}:section-{index}",
                title=first_words or f"Section {index}",
                chunk_ids=tuple(item.chunk_id for item in section_chunks),
                start_seconds=section_chunks[0].start,
                end_seconds=section_chunks[-1].end,
            )
        )
    return tuple(sections)


def _showrunner_response(
    response: Mapping[str, object],
    *,
    short_windows: Mapping[str, TranscriptWindow],
    long_windows: Mapping[str, TranscriptWindow],
    chunks: tuple[_SourceChunk, ...],
    expected_short_count: int,
) -> tuple[tuple[ShortDraft, ...], LongFormDraftPlan, str]:
    _exact_keys(
        response,
        {"shorts", "long_form", "selection_rationale"},
        "showrunner_response_schema_violation",
    )
    shorts_value = response.get("shorts")
    if not isinstance(shorts_value, list) or len(shorts_value) != expected_short_count:
        raise EditorialValidationError("showrunner_short_count_must_match_request")
    expected_short_keys = {
        "candidate_id",
        "start_chunk_id",
        "end_chunk_id",
        "start_seconds",
        "end_seconds",
        "angle",
        "rationale",
    }
    selected: list[ShortDraft] = []
    seen_ids: set[str] = set()
    seen_angles: set[str] = set()
    seen_text: set[str] = set()
    for raw in shorts_value:
        item = _as_mapping(raw, "showrunner_short_schema_violation")
        _exact_keys(item, expected_short_keys, "showrunner_short_schema_violation")
        window = _validate_reference(item, windows=short_windows, chunks=chunks)
        if window.window_id in seen_ids:
            raise EditorialValidationError("showrunner_duplicate_short_id")
        seen_ids.add(window.window_id)
        angle = _as_nonempty_string(item.get("angle"), "showrunner_short_angle_required")
        normalized_angle = " ".join(angle.lower().split())
        if normalized_angle in seen_angles:
            raise EditorialValidationError("showrunner_short_angles_must_be_diverse")
        seen_angles.add(normalized_angle)
        if any(
            window.start_seconds < previous.end_seconds
            and previous.start_seconds < window.end_seconds
            for previous in selected
        ):
            raise EditorialValidationError("showrunner_shorts_must_not_overlap")
        normalized_text = " ".join(window.text.lower().split())
        if normalized_text in seen_text:
            raise EditorialValidationError("showrunner_short_content_not_diverse")
        seen_text.add(normalized_text)
        selected.append(
            ShortDraft(
                candidate_id=window.window_id,
                chunk_ids=window.chunk_ids,
                start_seconds=window.start_seconds,
                end_seconds=window.end_seconds,
                text=window.text,
                angle=angle,
                rationale=_as_nonempty_string(
                    item.get("rationale"), "showrunner_short_rationale_required"
                ),
            )
        )
    long_value = _as_mapping(
        response.get("long_form"), "showrunner_long_form_schema_violation"
    )
    expected_long_keys = {
        "window_id",
        "start_chunk_id",
        "end_chunk_id",
        "start_seconds",
        "end_seconds",
        "title",
        "thesis",
        "rationale",
    }
    _exact_keys(long_value, expected_long_keys, "showrunner_long_form_schema_violation")
    long_window = _validate_reference(
        long_value,
        windows=long_windows,
        chunks=chunks,
        id_key="window_id",
    )
    if not 600 <= long_window.duration_seconds <= 900:
        raise EditorialValidationError("long_form_duration_must_be_10_to_15_minutes")
    long_form = LongFormDraftPlan(
        window_id=long_window.window_id,
        chunk_ids=long_window.chunk_ids,
        start_seconds=long_window.start_seconds,
        end_seconds=long_window.end_seconds,
        title=_as_nonempty_string(long_value.get("title"), "long_form_title_required"),
        thesis=_as_nonempty_string(long_value.get("thesis"), "long_form_thesis_required"),
        rationale=_as_nonempty_string(
            long_value.get("rationale"), "long_form_rationale_required"
        ),
        sections=_make_sections(long_window, chunks),
    )
    selection_rationale = _as_nonempty_string(
        response.get("selection_rationale"), "showrunner_selection_rationale_required"
    )
    return tuple(selected), long_form, selection_rationale


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secrets) for item in value]
    return value


class EditorialAgentTeam:
    """Run four independent scout lanes and a grounded Showrunner synthesis."""

    def __init__(self, transport: EditorialTransport) -> None:
        self.transport = transport
        self.last_trace: tuple[EditorialTraceRecord, ...] = ()

    def plan(
        self,
        chunks: Iterable[object],
        *,
        creator_preferences: Iterable[str] = (),
        short_count: int = 3,
        minimum_long_seconds: float = 600.0,
        maximum_long_seconds: float = 900.0,
        guard: RuntimeGuard | None = None,
        provider_consent_receipt: Mapping[str, object] | None = None,
        budget_reservation_receipt: Mapping[str, object] | None = None,
        secret_resolver: Callable[[str], str] | None = None,
        secret_ref: str = "keychain://ReelBrain/openai",
        secret_store_id: str = "reelbrain-keychain",
        secret_store_kind: str = "macos_keychain",
        secret_store_source: str = "ReelBrain/openai",
        checkpoint_dir=None,
        checkpoint_scope: str = "",
    ) -> EditorialPlan:
        if isinstance(short_count, bool) or not isinstance(short_count, int) or not 2 <= short_count <= 10:
            raise EditorialValidationError("short_count_must_be_2_to_10")
        if not 600 <= minimum_long_seconds <= maximum_long_seconds <= 900:
            raise EditorialValidationError("long_form_bounds_must_be_within_10_to_15_minutes")
        source_chunks = _coerce_chunks(chunks)
        short_candidates = build_short_candidate_windows(source_chunks)
        long_candidates = build_long_form_candidate_windows(
            source_chunks,
            min_seconds=minimum_long_seconds,
            max_seconds=maximum_long_seconds,
        )
        if len(short_candidates) < 2:
            raise EditorialValidationError("at_least_two_short_candidates_required")
        if _maximum_non_overlapping_window_count(short_candidates) < short_count:
            raise EditorialValidationError(
                "requested_non_overlapping_short_count_not_source_feasible"
            )
        if not long_candidates:
            raise EditorialValidationError("valid_10_to_15_minute_long_form_window_required")
        preferences = tuple(
            preference.strip()
            for preference in creator_preferences
            if isinstance(preference, str) and preference.strip()
        )
        checkpoint_root = None
        if checkpoint_dir is not None:
            from pathlib import Path

            if not checkpoint_scope.strip():
                raise EditorialValidationError("editorial_checkpoint_scope_required")
            checkpoint_root = Path(checkpoint_dir).expanduser().resolve()
            if guard is not None:
                guard.authorize_path(
                    checkpoint_root,
                    operation="write",
                    data_class="editorial_checkpoint_directory",
                )
            checkpoint_root.mkdir(parents=True, exist_ok=True)

        def dispatch(api_key: str | None = None) -> EditorialPlan:
            return self._plan_inside_dispatch(
                source_chunks,
                short_candidates,
                long_candidates,
                preferences,
                short_count,
                checkpoint_root=checkpoint_root,
                checkpoint_scope=checkpoint_scope,
                checkpoint_guard=guard,
                api_key=api_key,
            )

        metadata = self.transport.metadata
        if guard is None:
            if metadata.provider is not None:
                raise PermissionError("provider_editorial_transport_requires_runtime_guard")
            return dispatch()
        if metadata.provider is None:
            return guard.run_callback_tool(
                tool_id=metadata.tool_id,
                capability=metadata.capability,
                dispatch=dispatch,
                official=metadata.official,
            )
        return guard.run_callback_tool(
            tool_id=metadata.tool_id,
            capability=metadata.capability,
            dispatch=dispatch,
            official=metadata.official,
            provider=metadata.provider,
            consent_receipt=provider_consent_receipt,
            destination_host=metadata.destination_host,
            budget_reservation_receipt=budget_reservation_receipt,
            secret_ref=secret_ref,
            secret_store_id=secret_store_id,
            secret_store_kind=secret_store_kind,
            secret_store_source=secret_store_source,
            secret_resolver=secret_resolver,
            failure_budget_state="partially_consumed",
            tool_description=(
                "Run four read-only editorial persona lanes and a grounded Showrunner "
                "over creator-approved timestamped transcript candidates."
            ),
            input_schema={
                "type": "object",
                "required": ["timestamped_transcript", "candidate_catalog"],
            },
            data_effects=(
                "uploads timestamped transcript candidates to api.openai.com",
                "writes local persona and Showrunner checkpoints plus evaluation trace",
            ),
        )

    def _plan_inside_dispatch(
        self,
        chunks: tuple[_SourceChunk, ...],
        short_candidates: tuple[TranscriptWindow, ...],
        long_candidates: tuple[TranscriptWindow, ...],
        creator_preferences: tuple[str, ...],
        short_count: int,
        checkpoint_root,
        checkpoint_scope: str,
        checkpoint_guard: RuntimeGuard | None,
        *,
        api_key: str | None,
    ) -> EditorialPlan:
        records: list[EditorialTraceRecord] = []
        short_by_id = {window.window_id: window for window in short_candidates}
        long_by_id = {window.window_id: window for window in long_candidates}
        short_payload = [_window_payload(window) for window in short_candidates]
        lanes: list[PersonaLaneResult] = []
        for persona in _PERSONAS:
            response: dict[str, object] | None = None
            request_payload = {
                "agent": persona,
                "creator_preferences": list(creator_preferences),
                "tool_description": _PERSONA_TOOL_DESCRIPTION,
                "short_candidates": short_payload,
            }
            system_prompt = (
                "You are the ReelBrain "
                f"{persona}. {_PERSONA_PROMPTS[persona]} "
                "Return strict JSON and cite only supplied source-grounded IDs."
            )
            try:
                response = self._respond_with_checkpoint(
                    checkpoint_root=checkpoint_root,
                    checkpoint_scope=checkpoint_scope,
                    checkpoint_guard=checkpoint_guard,
                    checkpoint_name=f"persona-{persona.lower().replace(' ', '-')}.json",
                    system_prompt=system_prompt,
                    input_payload=request_payload,
                    json_schema=_PERSONA_SCHEMA,
                    schema_name=f"reelbrain_{persona.lower().replace(' ', '_')}",
                    api_key=api_key,
                )
                lane = _persona_response(
                    persona,
                    response,
                    windows=short_by_id,
                    chunks=chunks,
                )
            except Exception as exc:
                safe_response = (
                    _redact(response, (api_key or "",))
                    if isinstance(response, Mapping)
                    else {"transport_error_type": type(exc).__name__}
                )
                records.append(
                    self._trace_record(
                        records,
                        stage="persona_assessment",
                        agent=persona,
                        system_prompt=system_prompt,
                        tool_description=_PERSONA_TOOL_DESCRIPTION,
                        schema=_PERSONA_SCHEMA,
                        request=request_payload,
                        response=safe_response,
                        validation=f"rejected:{exc}",
                        rationales=(),
                    )
                )
                self.last_trace = tuple(records)
                raise
            lanes.append(lane)
            records.append(
                self._trace_record(
                    records,
                    stage="persona_assessment",
                    agent=persona,
                    system_prompt=system_prompt,
                    tool_description=_PERSONA_TOOL_DESCRIPTION,
                    schema=_PERSONA_SCHEMA,
                    request=request_payload,
                    response=_redact(response, (api_key or "",)),
                    validation="accepted",
                    rationales=tuple(item.rationale for item in lane.selections),
                )
            )

        showrunner_prompt = _showrunner_prompt(short_count)
        showrunner_schema = _showrunner_schema(short_count)
        showrunner_request = {
            "agent": "Showrunner",
            "requested_short_count": short_count,
            "creator_preferences": list(creator_preferences),
            "tool_description": _SHOWRUNNER_TOOL_DESCRIPTION,
            "source_chunks": _source_payload(chunks),
            "short_candidates": short_payload,
            "long_form_candidates": [
                _window_payload(window, include_text=False) for window in long_candidates
            ],
            "persona_responses": [asdict(lane) for lane in lanes],
        }
        showrunner_response: dict[str, object] | None = None
        try:
            showrunner_response = self._respond_with_checkpoint(
                checkpoint_root=checkpoint_root,
                checkpoint_scope=checkpoint_scope,
                checkpoint_guard=checkpoint_guard,
                checkpoint_name="showrunner.json",
                system_prompt=showrunner_prompt,
                input_payload=showrunner_request,
                json_schema=showrunner_schema,
                schema_name="reelbrain_showrunner_plan",
                api_key=api_key,
            )
            shorts, long_form, rationale = _showrunner_response(
                showrunner_response,
                short_windows=short_by_id,
                long_windows=long_by_id,
                chunks=chunks,
                expected_short_count=short_count,
            )
        except Exception as exc:
            safe_response = (
                _redact(showrunner_response, (api_key or "",))
                if isinstance(showrunner_response, Mapping)
                else {"transport_error_type": type(exc).__name__}
            )
            records.append(
                self._trace_record(
                    records,
                    stage="showrunner_synthesis",
                    agent="Showrunner",
                    system_prompt=showrunner_prompt,
                    tool_description=_SHOWRUNNER_TOOL_DESCRIPTION,
                    schema=showrunner_schema,
                    request=showrunner_request,
                    response=safe_response,
                    validation=f"rejected:{exc}",
                    rationales=(),
                )
            )
            self.last_trace = tuple(records)
            raise
        records.append(
            self._trace_record(
                records,
                stage="showrunner_synthesis",
                agent="Showrunner",
                system_prompt=showrunner_prompt,
                tool_description=_SHOWRUNNER_TOOL_DESCRIPTION,
                schema=showrunner_schema,
                request=showrunner_request,
                response=_redact(showrunner_response, (api_key or "",)),
                validation="accepted",
                rationales=(rationale, *(item.rationale for item in shorts), long_form.rationale),
            )
        )
        self.last_trace = tuple(records)
        return EditorialPlan(shorts, long_form, tuple(lanes), self.last_trace)

    def _respond_with_checkpoint(
        self,
        *,
        checkpoint_root,
        checkpoint_scope: str,
        checkpoint_guard: RuntimeGuard | None,
        checkpoint_name: str,
        system_prompt: str,
        input_payload: dict[str, object],
        json_schema: dict[str, object],
        schema_name: str,
        api_key: str | None,
    ) -> dict[str, object]:
        scope = {
            "checkpoint_scope": checkpoint_scope,
            "model": self.transport.metadata.model,
            "tool_id": self.transport.metadata.tool_id,
            "schema_name": schema_name,
            "request_sha256": sha256(
                json.dumps(
                    {
                        "system_prompt": system_prompt,
                        "input_payload": input_payload,
                        "json_schema": json_schema,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        path = checkpoint_root / checkpoint_name if checkpoint_root is not None else None
        if path is not None and path.is_file():
            if checkpoint_guard is not None:
                checkpoint_guard.authorize_path(
                    path, operation="read", data_class="editorial_checkpoint"
                )
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                response = document["response"]
                response_digest = sha256(
                    json.dumps(response, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if (
                    document.get("scope") == scope
                    and document.get("response_sha256") == response_digest
                    and isinstance(response, dict)
                ):
                    return response
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                pass
        response = self.transport.respond(
            system_prompt=system_prompt,
            input_payload=input_payload,
            json_schema=json_schema,
            schema_name=schema_name,
            api_key=api_key,
        )
        if path is not None:
            if checkpoint_guard is not None:
                checkpoint_guard.authorize_path(
                    path, operation="write", data_class="editorial_checkpoint"
                )
            response_digest = sha256(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            temporary = path.with_suffix(path.suffix + ".tmp")
            if checkpoint_guard is not None:
                checkpoint_guard.authorize_path(
                    temporary,
                    operation="write",
                    data_class="editorial_checkpoint_temporary",
                )
            temporary.write_text(
                json.dumps(
                    {
                        "scope": scope,
                        "response": response,
                        "response_sha256": response_digest,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        return response

    def _trace_record(
        self,
        records: list[EditorialTraceRecord],
        *,
        stage: Literal["persona_assessment", "showrunner_synthesis"],
        agent: str,
        system_prompt: str,
        tool_description: str,
        schema: dict[str, object],
        request: dict[str, object],
        response: object,
        validation: str,
        rationales: tuple[str, ...],
    ) -> EditorialTraceRecord:
        response_payload = (
            dict(response) if isinstance(response, Mapping) else {"response": response}
        )
        return EditorialTraceRecord(
            sequence=len(records) + 1,
            stage=stage,
            agent=agent,
            system_prompt=system_prompt,
            tool_description=tool_description,
            response_schema=json.loads(json.dumps(schema)),
            request_payload=json.loads(json.dumps(request, ensure_ascii=False)),
            response_payload=json.loads(json.dumps(response_payload, ensure_ascii=False)),
            validation=validation,
            selection_rationale=rationales,
            provider_metadata=asdict(self.transport.metadata),
        )
