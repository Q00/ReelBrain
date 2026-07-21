import json

import pytest

from reelbrain.agent_tools import AgentToolExecutor
from reelbrain.toolbox import ToolboxManager


def executor(tmp_path):
    return AgentToolExecutor(
        project_id="project-1",
        creator_id="creator-1",
        workspace_root=tmp_path / "run",
        read_roots=(tmp_path,),
        toolbox=ToolboxManager(tmp_path / ".ReelBrain" / "toolbox"),
    )


def test_agent_invokes_semantic_tool_while_package_is_only_a_dependency(tmp_path):
    tools = executor(tmp_path)

    result = tools.invoke(
        agent="assembler",
        tool_id="render-vertical-short",
        payload={
            "source_id": "source-01",
            "output_id": "short-01",
            "start_seconds": 10,
            "duration_seconds": 40,
        },
        dispatch=lambda: {"video": "short-01.mp4"},
    )

    assert result == {"video": "short-01.mp4"}
    call = tools.calls[0]
    assert call.agent_id == "assembler"
    assert call.tool_id == "render-vertical-short"
    assert "pillow>=12.2,<13" in call.implementation_dependencies
    assert call.status == "completed_claim_pending_independent_verification"
    descriptor = tools.toolbox.resolve_active("render-vertical-short")
    document = json.loads(descriptor.artifact_path.read_text(encoding="utf-8"))
    assert document["description"].startswith("Render one approved")
    assert document["implementation_dependencies"] == [
        "ffmpeg",
        "ffprobe",
        "pillow>=12.2,<13",
    ]


def test_agent_cannot_call_tool_owned_by_another_role(tmp_path):
    tools = executor(tmp_path)

    with pytest.raises(PermissionError, match="agent_not_allowed_to_invoke_tool"):
        tools.invoke(
            agent="meaning-scout",
            tool_id="render-long-form",
            payload={"source_id": "source-01", "output_id": "long-01", "segments": []},
            dispatch=lambda: None,
        )


def test_timed_image_overlay_is_a_default_semantic_tool(tmp_path):
    tools = executor(tmp_path)

    result = tools.invoke(
        agent="assembler",
        tool_id="overlay-timed-image",
        payload={
            "source_id": "source-01",
            "output_id": "long-01",
            "image_path": "diagram.png",
            "start_seconds": 655,
            "end_seconds": 670,
        },
        dispatch=lambda: {"video": "long-01-with-overlay.mp4"},
    )

    assert result == {"video": "long-01-with-overlay.mp4"}
    call = tools.calls[0]
    assert call.tool_id == "overlay-timed-image"
    assert call.capability == "media:overlay-image"
    assert call.implementation_dependencies == ("ffmpeg", "ffprobe")


def test_four_visible_editors_have_the_tools_shown_in_desktop_configuration(tmp_path):
    tools = executor(tmp_path)

    expected = {
        "meaning-scout": {"analyze-story-structure", "transcribe-bilingual"},
        "hook-scout": {"analyze-retention", "render-vertical-short"},
        "creator-advocate": {
            "apply-creator-taste",
            "overlay-timed-image",
            "design-thumbnail",
        },
        "context-guardian": {"validate-context-continuity", "render-long-form"},
    }

    for agent, tool_ids in expected.items():
        assert tool_ids == {
            contract.tool_id
            for contract in tools.contracts.values()
            if agent in contract.allowed_agents
        }


def test_execution_contract_and_trace_are_inspectable(tmp_path):
    tools = executor(tmp_path)
    contract = tools.write_execution_contract(tmp_path / "contract.json")
    tools.invoke(
        agent="showrunner",
        tool_id="plan-editorial-candidates",
        payload={
            "source_id": "source-01",
            "transcript_path": "transcript.json",
            "short_count": 3,
        },
        dispatch=lambda: {"shorts": 3},
    )
    trace = tools.write_trace(tmp_path / "trace.json")

    contract_document = json.loads(contract.read_text(encoding="utf-8"))
    trace_document = json.loads(trace.read_text(encoding="utf-8"))
    assert contract_document["principle"] == (
        "agents invoke semantic tools; packages are bound implementations"
    )
    assert trace_document["claim_is_not_confirmation"] is True
    assert trace_document["calls"][0]["agent_id"] == "showrunner"
