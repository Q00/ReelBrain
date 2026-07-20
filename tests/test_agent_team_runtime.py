from pathlib import Path

import pytest

from reelbrain.agent_runtime import AGENTS, AgentTeamRuntime
from reelbrain.toolbox import ManifestSigner, ToolManifest, ToolboxManager, sha256_file


def runtime(tmp_path):
    return AgentTeamRuntime(
        project_id="project-1",
        creator_id="creator-1",
        toolbox=ToolboxManager(tmp_path / ".ReelBrain" / "toolbox"),
    )


def test_creator_steering_invalidates_stale_agent_results(tmp_path):
    team = runtime(tmp_path)
    task = team.submit_task(
        agent="meaning-scout",
        task_type="find-highlights",
        payload={"source": "video.mp4"},
    )

    event = team.steer("Preserve the full technical caveat", target_agent="meaning-scout")

    assert event.current_epoch == event.previous_epoch + 1
    with pytest.raises(ValueError, match="stale_workflow_epoch"):
        team.complete_task(task, {"candidate": "stale"})
    replacement = team.submit_task(
        agent="meaning-scout",
        task_type="find-highlights",
        payload={"source": "video.mp4", "steering": event.message},
    )
    team.complete_task(replacement, {"candidate": "current"})
    assert team.results[replacement.task_id] == {"candidate": "current"}


def test_cancel_prevents_new_or_completed_agent_work(tmp_path):
    team = runtime(tmp_path)
    task = team.submit_task(agent="hook-scout", task_type="rank-hooks", payload={})

    team.cancel("creator stopped the edit")

    with pytest.raises(RuntimeError, match="agent_team_cancelled"):
        team.complete_task(task, {})
    with pytest.raises(RuntimeError, match="agent_team_cancelled"):
        team.submit_task(agent="showrunner", task_type="select", payload={})


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_can_request_a_missing_tool(agent, tmp_path):
    team = runtime(tmp_path)

    request = team.request_tool(
        agent=agent,
        description="Need exact caption layout validation",
        capabilities=("caption:layout",),
    )

    assert request.requesting_agent == agent
    assert request.status == "toolsmith_required"


def test_only_toolsmith_can_stage_and_human_gate_controls_activation(tmp_path):
    team = runtime(tmp_path)
    request = team.request_tool(
        agent="context-guardian",
        description="Need slide-safe crop validation",
        capabilities=("reframe:validate",),
    )
    artifact = tmp_path / "generated-tool"
    artifact.write_text("implementation", encoding="utf-8")
    manifest = ToolManifest(
        tool_id="slide-safe-crop",
        version="0.1.0",
        digest=sha256_file(artifact),
        origin="generated",
        entrypoint="slide-safe-crop",
        capabilities=("reframe:validate",),
    )

    with pytest.raises(PermissionError, match="only_toolsmith_can_stage_tools"):
        team.toolsmith_stage(
            request.request_id,
            acting_agent="showrunner",
            artifact=artifact,
            manifest=manifest,
        )
    team.toolsmith_stage(
        request.request_id,
        acting_agent="toolsmith",
        artifact=artifact,
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="tool_auditor_pass_required"):
        team.human_approve_tool(
            request.request_id,
            human_approver_id="human:creator-1",
            approval_receipt_id="approval-1",
            auditor_report={"passed": False},
        )

    approved = team.human_approve_tool(
        request.request_id,
        human_approver_id="human:creator-1",
        approval_receipt_id="approval-1",
        auditor_report={"passed": True, "sandbox": True, "rollback": True},
    )

    assert approved.manifest.state == "approved"
    assert team.tool_requests[request.request_id].status == "approved"


def test_equivalent_approved_tool_is_reused_before_toolsmith_generation(tmp_path):
    team = runtime(tmp_path)
    artifact = tmp_path / "official-caption-tool"
    artifact.write_text("official", encoding="utf-8")
    signer = ManifestSigner(key_id="release", key=b"release-key")
    manifest = signer.sign(
        ToolManifest(
            tool_id="official-caption-layout",
            version="1.0.0",
            digest=sha256_file(artifact),
            origin="official",
            entrypoint="official-caption-layout",
            capabilities=("caption:layout",),
        )
    )
    team.toolbox.install_official(
        artifact, manifest, signer=signer, conformance=lambda *_: True
    )

    request = team.request_tool(
        agent="showrunner",
        description="Need caption layout",
        capabilities=("caption:layout",),
    )

    assert request.status == "reuse_approved_tool"
    assert request.equivalent_tool_id == "official-caption-layout"
