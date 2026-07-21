from dataclasses import asdict
import json

import pytest

from reelbrain.editorial import (
    EditorialAgentTeam,
    EditorialValidationError,
    OpenAIResponsesHTTPTransport,
    ProviderMetadata,
    build_long_form_candidate_windows,
    build_short_candidate_windows,
)
from reelbrain.runtime_guard import RuntimeGuard
from reelbrain.transcription import TranscriptChunk


def transcript(*, split_sentences: bool = False):
    chunks = []
    for index in range(120):
        if split_sentences:
            text = (
                f"Lesson {index // 2} introduces a governed agent principle"
                if index % 2 == 0
                else f"and completes it with practical takeaway {index // 2}."
            )
        else:
            text = (
                f"Lesson {index} explains governed agent concept {index} with "
                f"a distinct practical takeaway {index}."
            )
        chunks.append(
            TranscriptChunk(
                chunk_id=f"chunk-{index:03}",
                start=index * 10.0,
                end=(index + 1) * 10.0,
                text=text,
                confidence=0.98,
            )
        )
    return tuple(chunks)


def reference(candidate, *, id_key="candidate_id"):
    return {
        id_key: candidate["candidate_id" if id_key == "candidate_id" else "window_id"],
        "start_chunk_id": candidate["start_chunk_id"],
        "end_chunk_id": candidate["end_chunk_id"],
        "start_seconds": candidate["start_seconds"],
        "end_seconds": candidate["end_seconds"],
    }


class ScriptedTransport:
    metadata = ProviderMetadata(
        tool_id="fixture-editorial",
        capability="editorial:plan",
        provider=None,
        destination_host=None,
        official=True,
        model="fixture",
    )

    def __init__(self, mode="valid", *, metadata=None):
        self.mode = mode
        self.calls = []
        if metadata is not None:
            self.metadata = metadata

    def respond(
        self,
        *,
        system_prompt,
        input_payload,
        json_schema,
        schema_name,
        api_key=None,
    ):
        self.calls.append(
            {
                "agent": input_payload["agent"],
                "api_key": api_key,
                "schema": json_schema,
                "schema_name": schema_name,
                "system_prompt": system_prompt,
            }
        )
        if input_payload["agent"] != "Showrunner":
            candidate = input_payload["short_candidates"][0]
            item = {
                **reference(candidate),
                "score": 0.91,
                "rationale": f"{input_payload['agent']} found grounded value",
                "risks": [],
            }
            if self.mode == "invented_persona_id":
                item["candidate_id"] = "short-invented"
            selections = [item]
            if self.mode == "mixed_persona_ids":
                selections.append({**item, "candidate_id": "short-invented"})
            return {"selections": selections}

        shorts = input_payload["short_candidates"]
        requested_count = input_payload["requested_short_count"]
        picked = []
        for candidate in shorts:
            if not picked or candidate["start_seconds"] >= picked[-1]["end_seconds"]:
                picked.append(candidate)
            if len(picked) == requested_count:
                break
        assert len(picked) == requested_count
        first = picked[0]
        if self.mode == "overlap":
            picked[1] = shorts[1]
        short_items = [
            {
                **reference(candidate),
                "angle": f"editorial angle {index}",
                "rationale": f"grounded source moment {index} with a distinct payoff",
            }
            for index, candidate in enumerate(picked, start=1)
        ]
        first_item = short_items[0]
        if self.mode == "invented_timestamp":
            first_item["start_seconds"] += 1.0
        if self.mode == "mid_thought_endpoint":
            incomplete = next(
                chunk
                for chunk in input_payload["source_chunks"]
                if not chunk["text"].rstrip().endswith((".", "!", "?", "。", "！", "？"))
                and chunk["chunk_id"] in first["chunk_ids"]
            )
            first_item["end_chunk_id"] = incomplete["chunk_id"]
            first_item["end_seconds"] = incomplete["end_seconds"]
        if self.mode == "wrong_count":
            short_items.pop()
        long_form = input_payload["long_form_candidates"][0]
        return {
            "shorts": short_items,
            "long_form": {
                **reference(long_form, id_key="window_id"),
                "title": "How governed agents actually work",
                "thesis": "Agent behavior needs grounded evidence and explicit control.",
                "rationale": "The contiguous source span develops one complete argument.",
            },
            "selection_rationale": "Balances meaning, hook, creator voice, and context.",
        }


def test_builds_only_grounded_natural_short_and_long_windows():
    chunks = transcript(split_sentences=True)

    shorts = build_short_candidate_windows(chunks)
    longs = build_long_form_candidate_windows(chunks)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}

    assert shorts
    assert longs
    assert all(30 <= item.duration_seconds <= 60 for item in shorts)
    assert all(600 <= item.duration_seconds <= 900 for item in longs)
    assert all(by_id[item.end_chunk_id].text.endswith(".") for item in (*shorts, *longs))
    assert all(
        item.start_chunk_id == chunks[0].chunk_id
        or by_id[f"chunk-{int(item.start_chunk_id.split('-')[1]) - 1:03}"].text.endswith(".")
        for item in (*shorts, *longs)
    )


def test_fans_out_four_personas_then_builds_inspectable_showrunner_plan():
    transport = ScriptedTransport()

    plan = EditorialAgentTeam(transport).plan(
        transcript(), creator_preferences=("technical, not sensational",)
    )

    assert [call["agent"] for call in transport.calls] == [
        "Story Editor",
        "Retention Editor",
        "Style Editor",
        "Continuity Editor",
        "Showrunner",
    ]
    assert transport.calls[-1]["schema"]["properties"]["shorts"]["minItems"] == 3
    assert transport.calls[-1]["schema"]["properties"]["shorts"]["maxItems"] == 3
    assert len(plan.persona_selections) == 4
    assert len(plan.shorts) == 3
    assert all(
        previous.end_seconds <= current.start_seconds
        for previous, current in zip(plan.shorts, plan.shorts[1:])
    )
    assert all(30 <= short.duration_seconds <= 60 for short in plan.shorts)
    assert 600 <= plan.long_form.duration_seconds <= 900
    assert plan.long_form.sections
    assert plan.long_form.sections[0].start_seconds == plan.long_form.start_seconds
    assert plan.long_form.sections[-1].end_seconds == plan.long_form.end_seconds
    assert [record.sequence for record in plan.trace] == [1, 2, 3, 4, 5]
    assert all(record.system_prompt for record in plan.trace)
    assert all(record.tool_description for record in plan.trace)
    assert all(record.response_schema["additionalProperties"] is False for record in plan.trace)
    assert all(record.validation == "accepted" for record in plan.trace)
    assert plan.trace[-1].selection_rationale


@pytest.mark.parametrize("short_count", (1, 11, True, 3.5))
def test_short_count_must_stay_within_product_bounds(short_count):
    with pytest.raises(EditorialValidationError, match="short_count_must_be_2_to_10"):
        EditorialAgentTeam(ScriptedTransport()).plan(
            transcript(), short_count=short_count
        )


@pytest.mark.parametrize(
    "minimum,maximum",
    ((599, 900), (600, 901), (800, 700)),
)
def test_long_form_bounds_must_remain_within_requested_10_to_15_minutes(
    minimum, maximum
):
    with pytest.raises(EditorialValidationError, match="long_form_bounds"):
        EditorialAgentTeam(ScriptedTransport()).plan(
            transcript(),
            minimum_long_seconds=minimum,
            maximum_long_seconds=maximum,
        )


@pytest.mark.parametrize(
    ("mode", "message", "split_sentences"),
    [
        ("invented_persona_id", "persona_requires_grounded_selection", False),
        ("overlap", "showrunner_shorts_must_not_overlap", False),
        ("wrong_count", "showrunner_short_count_must_match_request", False),
    ],
)
def test_rejects_ungrounded_or_invalid_agent_selections(mode, message, split_sentences):
    team = EditorialAgentTeam(ScriptedTransport(mode))

    with pytest.raises(EditorialValidationError, match=message):
        team.plan(transcript(split_sentences=split_sentences))

    assert team.last_trace[-1].validation == f"rejected:{message}"
    assert team.last_trace[-1].response_payload


@pytest.mark.parametrize("mode", ("invented_timestamp", "mid_thought_endpoint"))
def test_candidate_id_is_authoritative_over_redundant_boundary_claims(mode):
    chunks = transcript(split_sentences=mode == "mid_thought_endpoint")
    windows = {item.window_id: item for item in build_short_candidate_windows(chunks)}
    plan = EditorialAgentTeam(ScriptedTransport(mode)).plan(chunks)

    selected = plan.shorts[0]
    canonical = windows[selected.candidate_id]
    assert selected.candidate_id.startswith("short-")
    assert 30 <= selected.duration_seconds <= 60
    assert selected.chunk_ids == canonical.chunk_ids
    assert selected.start_seconds == canonical.start_seconds
    assert selected.end_seconds == canonical.end_seconds


def test_persona_lane_discards_invented_reference_when_grounded_selection_remains():
    plan = EditorialAgentTeam(ScriptedTransport("mixed_persona_ids")).plan(transcript())

    assert all(len(lane.selections) == 1 for lane in plan.persona_selections)
    assert all(
        selection.candidate_id.startswith("short-")
        for lane in plan.persona_selections
        for selection in lane.selections
    )


class FakeHTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return b'{"output_text":"{\\"answer\\":\\"grounded\\"}"}'


def test_openai_responses_transport_requests_strict_json_without_network():
    captured = {}

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHTTPResponse()

    transport = OpenAIResponsesHTTPTransport(model="gpt-test", opener=opener)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    result = transport.respond(
        system_prompt="Use only supplied evidence.",
        input_payload={"candidate_ids": ["short-1"]},
        json_schema=schema,
        schema_name="editorial_fixture",
        api_key="dispatch-only-key",
    )

    request = captured["request"]
    body = json.loads(request.data)
    assert result == {"answer": "grounded"}
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_header("Authorization") == "Bearer dispatch-only-key"
    assert body["model"] == "gpt-test"
    assert body["store"] is False
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "editorial_fixture",
        "strict": True,
        "schema": schema,
    }


def test_provider_key_exists_only_inside_single_runtime_guard_dispatch(tmp_path):
    metadata = ProviderMetadata(
        tool_id="openai-responses-editorial",
        capability="editorial:plan",
        provider="openai",
        destination_host="api.openai.com",
        official=True,
        model="fixture-openai",
    )
    transport = ScriptedTransport(metadata=metadata)
    team = EditorialAgentTeam(transport)
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    consent = {
        "provider": "openai",
        "tool_id": "openai-responses-editorial",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "destination": "api.openai.com",
        "invocation_id": "editorial-plan-1",
        "approval_receipt_id": "provider-consent-editorial-1",
        "data_categories": ["transcript", "creator_preferences"],
        "purpose": "editorial planning",
        "expected_retention": "provider request lifecycle",
        "expected_cost": "one approved editorial planning dispatch",
    }
    budget = {
        "reservation_id": "budget-editorial-1",
        "requester_id": "reelbrain-runtime",
        "session_id": "runtime:project-1",
        "tool_id": "openai-responses-editorial",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "capabilities": ["editorial:plan"],
        "reserved_amount_cents": 25,
        "metered_units": 1,
        "cost_authorization_receipt_id": "cost-approved-editorial-1",
        "state": "reserved",
    }

    plan = team.plan(
        transcript(),
        guard=guard,
        provider_consent_receipt=consent,
        budget_reservation_receipt=budget,
        secret_resolver=lambda reference: "dispatch-secret",
        secret_ref="env://project/OPEN_API_KEY",
        secret_store_id="dogfood-dotenv-ephemeral",
        secret_store_kind="process_memory_dotenv",
        secret_store_source="project .env resolved only inside dispatch",
    )

    assert len(plan.trace) == 5
    assert [call["api_key"] for call in transport.calls] == ["dispatch-secret"] * 5
    serialized_trace = json.dumps([asdict(record) for record in plan.trace])
    assert "dispatch-secret" not in serialized_trace
    assert "dispatch-secret" not in json.dumps(guard.capability_receipts)
    assert [record["state"] for record in guard.budget_ledger] == ["reserved", "consumed"]


def test_editorial_checkpoints_replay_validated_responses_without_transport_calls(
    tmp_path,
):
    checkpoint_dir = tmp_path / "editorial-checkpoints"
    first_transport = ScriptedTransport()
    first = EditorialAgentTeam(first_transport).plan(
        transcript(),
        checkpoint_dir=checkpoint_dir,
        checkpoint_scope="source-config-plan-digest",
    )

    class NoCallTransport(ScriptedTransport):
        def respond(self, **kwargs):
            raise AssertionError("validated checkpoint should prevent provider replay")

    second_transport = NoCallTransport()
    second = EditorialAgentTeam(second_transport).plan(
        transcript(),
        checkpoint_dir=checkpoint_dir,
        checkpoint_scope="source-config-plan-digest",
    )

    assert len(first_transport.calls) == 5
    assert second_transport.calls == []
    assert [item.candidate_id for item in second.shorts] == [
        item.candidate_id for item in first.shorts
    ]
