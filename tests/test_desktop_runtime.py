from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reelbrain.desktop_state import (
    DesktopMemoryService,
    inspect_review_actions,
    record_review_action,
)
from reelbrain.desktop_bridge import dispatch
from reelbrain.fanout import GovernedFanoutService, digest


def make_supported_workspace(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"creator-owned-video")
    source_digest = sha256(source.read_bytes()).hexdigest()
    run = tmp_path / ".reelbrain" / "dogfood" / "2026-07-21" / "run"
    source_root = run / "source-01"
    source_root.mkdir(parents=True)
    (run / "source_inventory.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "source-01",
                    "path": str(source),
                    "sha256": source_digest,
                }
            ]
        ),
        encoding="utf-8",
    )
    transcript = {"korean": [{"chunk_id": "chunk-1", "text": "evidence"}]}
    (source_root / "bilingual_transcript.json").write_text(
        json.dumps(transcript), encoding="utf-8"
    )
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "start_chunk_id": f"chunk-{index}",
            "end_chunk_id": f"chunk-{index}",
            "start_seconds": float(index * 40),
            "end_seconds": float(index * 40 + 35),
            "duration_seconds": 35.0,
            "confidence": 0.95,
            "text": f"Self-contained educational candidate {index}",
        }
        for index in range(1, 5)
    ]
    (source_root / "editorial_plan.json").write_text(
        json.dumps(
            {
                "trace": [
                    {"request_payload": {"short_candidates": candidates}}
                ],
                "shorts": candidates,
                "long_form": None,
            }
        ),
        encoding="utf-8",
    )
    return source, source_digest


def test_desktop_memory_is_durable_editable_and_deletion_fenced(tmp_path: Path) -> None:
    service = DesktopMemoryService(tmp_path)
    initial = service.inspect()
    assert initial["revision"] == 1
    assert len(initial["preferences"]) == 3

    remembered = service.mutate(
        {
            "action": "remember",
            "expected_revision": 1,
            "category": "Pacing",
            "value": "Use deliberate pauses",
            "scope": {"output_mode": "short"},
            "creator_statement": "Remember this pacing preference.",
        }
    )
    preference = next(
        item for item in remembered["preferences"] if item["category"] == "Pacing"
    )
    edited = service.mutate(
        {
            "action": "edit",
            "expected_revision": remembered["revision"],
            "preference_id": preference["id"],
            "value": "Use deliberate pauses before the payoff",
            "scope": {"output_mode": "short", "content_kind": "technical"},
            "creator_statement": "Correct the pacing preference and its scope.",
        }
    )
    edited_preference = next(
        item for item in edited["preferences"] if item["id"] == preference["id"]
    )
    assert edited_preference["value"] == "Use deliberate pauses before the payoff"
    assert edited_preference["scope"]["content_kind"] == "technical"
    assert edited_preference["version"] == 2
    reloaded = DesktopMemoryService(tmp_path).inspect()
    assert any(item["id"] == preference["id"] for item in reloaded["preferences"])

    disabled = service.mutate(
        {
            "action": "disable",
            "expected_revision": reloaded["revision"],
            "preference_id": preference["id"],
            "creator_statement": "Disable this preference.",
        }
    )
    assert next(item for item in disabled["preferences"] if item["id"] == preference["id"])[
        "status"
    ] == "disabled"
    deleted = service.mutate(
        {
            "action": "delete",
            "expected_revision": disabled["revision"],
            "preference_id": preference["id"],
            "creator_statement": "Forget this preference permanently.",
        }
    )
    assert not any(item["id"] == preference["id"] for item in deleted["preferences"])
    assert any(item["preference_id"] == preference["id"] for item in deleted["tombstones"])


def test_creator_liked_draft_is_linked_as_external_preference_provenance(
    tmp_path: Path,
) -> None:
    service = DesktopMemoryService(tmp_path)
    state = service.inspect()
    evidence_event_id = "revision_feedback_1234567890abcdef"
    remembered = service.mutate(
        {
            "action": "remember",
            "expected_revision": state["revision"],
            "category": "Approved short edit",
            "value": "Open on the technical payoff, then explain why.",
            "scope": {"output_mode": "short"},
            "creator_statement": "I liked draft v2.",
            "source_evidence_event_id": evidence_event_id,
        }
    )
    preference = next(
        item
        for item in remembered["preferences"]
        if item["category"] == "Approved short edit"
    )
    assert evidence_event_id in preference["provenance_event_ids"]
    assert any(
        str(event_id).startswith("feedback_")
        for event_id in preference["provenance_event_ids"]
    )

    reloaded = DesktopMemoryService(tmp_path).inspect()
    reloaded_preference = next(
        item
        for item in reloaded["preferences"]
        if item["category"] == "Approved short edit"
    )
    assert evidence_event_id in reloaded_preference["provenance_event_ids"]


def test_desktop_memory_rejects_empty_preference_edits(tmp_path: Path) -> None:
    service = DesktopMemoryService(tmp_path)
    state = service.inspect()
    preference = state["preferences"][0]
    with pytest.raises(ValueError, match="preference_value_required"):
        service.mutate(
            {
                "action": "edit",
                "expected_revision": state["revision"],
                "preference_id": preference["id"],
                "value": "   ",
                "creator_statement": "An empty correction must not be stored.",
            }
        )


def test_desktop_bridge_keeps_generated_tool_quarantined_until_passing_audit_and_human_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELBRAIN_HOME", str(tmp_path / ".ReelBrain"))
    artifact = tmp_path / "tool.py"
    artifact.write_text(
        "import json, sys\nprint(json.dumps({'ok': True, 'input': json.loads(sys.stdin.readline())}))\n",
        encoding="utf-8",
    )

    staged = dispatch(
        "tool_stage_generated",
        {
            "approval_id": "approval-1",
            "tool_id": "caption-safe-area",
            "artifact_path": str(artifact),
            "capabilities": ["caption:safe-area"],
            "dependencies": [],
        },
        tmp_path,
    )
    assert staged["status"] == "quarantined"

    with pytest.raises(ValueError, match="tool_auditor_pass_required"):
        dispatch(
            "tool_deploy_approved",
            {
                "approval_id": "approval-1",
                "approval_receipt_id": "deploy-1",
                "auditor_report": {"passed": False, "summary": "tests failed"},
            },
            tmp_path,
        )

    deployed = dispatch(
        "tool_deploy_approved",
        {
            "approval_id": "approval-1",
            "approval_receipt_id": "deploy-2",
            "auditor_report": {
                "passed": True,
                "summary": "unittest suite passed",
                "test_command": "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py",
            },
        },
        tmp_path,
    )
    assert deployed["status"] == "deployed"
    deployed_artifact = Path(deployed["artifact_path"])
    assert deployed_artifact.is_file()
    execution = subprocess.run(
        [sys.executable, str(deployed_artifact)],
        input=json.dumps({"width": 1080, "height": 1920}) + "\n",
        text=True,
        capture_output=True,
        check=True,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert json.loads(execution.stdout) == {
        "ok": True,
        "input": {"width": 1080, "height": 1920},
    }


def test_two_episode_examples_create_confirmable_proposal(tmp_path: Path) -> None:
    service = DesktopMemoryService(tmp_path)
    state = service.inspect()
    for _ in range(2):
        state = service.mutate(
            {
                "action": "episode",
                "expected_revision": state["revision"],
                "category": "Visual Style",
                "value": "Centered source over restrained blur",
                "scope": {"output_mode": "short"},
                "creator_statement": "This edit should use the centered source treatment.",
            }
        )
    assert len(state["proposals"]) == 1
    confirmed = service.mutate(
        {
            "action": "confirm",
            "expected_revision": state["revision"],
            "proposal_id": state["proposals"][0]["proposal_id"],
            "creator_statement": "Confirm this learned preference.",
        }
    )
    assert any(
        item["category"] == "Visual Style" and item["explicit"] is False
        for item in confirmed["preferences"]
    )


def test_governed_fanout_grants_context_validates_results_and_hash_chain(
    tmp_path: Path,
) -> None:
    source, source_digest = make_supported_workspace(tmp_path)
    service = GovernedFanoutService(tmp_path)
    plan = service.plan(
        {
            "source_path": str(source),
            "source_sha256": source_digest,
            "creator_id": "creator-founder",
            "project_id": "project-1",
            "current_steering": "Keep the complete caveat even if the edit becomes longer.",
        }
    )
    assert plan["status"] == "READY_FOR_HOST_DISPATCH"
    assert len(plan["tasks"]) == 4
    assert len({task["capability_packet"]["grant_id"] for task in plan["tasks"]}) == 4

    results = []
    for task in plan["tasks"]:
        context = service.context(
            {
                "fanout_id": plan["fanout_id"],
                "task_id": task["task_id"],
                "capability_token": task["capability_packet"]["token"],
            }
        )
        assert context["current_steering"] == (
            "Keep the complete caveat even if the edit becomes longer."
        )
        assert context["memory_principle"] == (
            "Memory is a behavioral prior, never source evidence."
        )
        assert all(item["status"] == "active" for item in context["preferences"])
        candidate = context["candidates"][0]
        preference_id = context["preferences"][0]["id"]
        results.append(
            {
                "task_id": task["task_id"],
                "persona": task["persona"],
                "epoch": plan["epoch"],
                "snapshot_digest": plan["snapshot_digest"],
                "memory_snapshot_digest": plan["memory_snapshot_digest"],
                "selections": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "score": 0.9,
                        "rationale": "Grounded and complete.",
                        "risks": [],
                        "used_preference_ids": [preference_id],
                    }
                ],
            }
        )
    submitted = service.submit(
        {
            "fanout_id": plan["fanout_id"],
            "root_capability_token": plan["root_authority"]["token"],
            "results": results,
        }
    )
    assert submitted["status"] == "READY_FOR_RENDER_APPROVAL"
    assert submitted["publish_ready"] is False
    assert service.verify_evidence(plan["fanout_id"])["valid"] is True
    editorial_plan = json.loads(Path(submitted["plan_path"]).read_text(encoding="utf-8"))
    assert editorial_plan["used_preference_ids"]

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".reelbrain" / "desktop" / "fanout").rglob("*.json*")
    )
    for task in plan["tasks"]:
        assert task["capability_packet"]["token"] not in persisted
    assert plan["root_authority"]["token"] not in persisted


def test_fanout_rejects_unrecognized_preference_attribution() -> None:
    with pytest.raises(ValueError, match="unknown_preference_id"):
        GovernedFanoutService._validate_result(
            {
                "task_id": "task-1",
                "persona": "meaning-scout",
                "epoch": 1,
                "snapshot_digest": "catalog-digest",
                "memory_snapshot_digest": "memory-digest",
                "selections": [
                    {
                        "candidate_id": "candidate-1",
                        "score": 0.9,
                        "rationale": "Complete and grounded.",
                        "risks": [],
                        "used_preference_ids": ["pref-invented"],
                    }
                ],
            },
            expected_task_id="task-1",
            expected_persona="meaning-scout",
            projection={
                "epoch": 1,
                "catalog_sha256": "catalog-digest",
                "memory_snapshot_digest": "memory-digest",
            },
            allowed_ids={"candidate-1"},
            allowed_preference_ids={"pref-real"},
        )


def test_fanout_denies_unknown_candidate_before_submission(tmp_path: Path) -> None:
    source, source_digest = make_supported_workspace(tmp_path)
    service = GovernedFanoutService(tmp_path)
    plan = service.plan(
        {"source_path": str(source), "source_sha256": source_digest}
    )
    task = plan["tasks"][0]
    with pytest.raises(ValueError, match="capability_candidate_scope_denied"):
        service.context(
            {
                "fanout_id": plan["fanout_id"],
                "task_id": task["task_id"],
                "capability_token": task["capability_packet"]["token"],
                "candidate_ids": ["invented-candidate"],
            }
        )
    evidence = service.evidence()
    assert any(
        event["reason_code"] == "capability_candidate_scope_denied"
        for event in evidence["events"]
    )


def test_steering_advances_epoch_and_revokes_previous_grants(tmp_path: Path) -> None:
    source, source_digest = make_supported_workspace(tmp_path)
    service = GovernedFanoutService(tmp_path)
    plan = service.plan(
        {"source_path": str(source), "source_sha256": source_digest}
    )
    steered = service.steer(
        {
            "fanout_id": plan["fanout_id"],
            "root_capability_token": plan["root_authority"]["token"],
            "action": "steer",
            "message": "Preserve the full caveat.",
        }
    )
    assert steered["current_epoch"] == 2
    task = plan["tasks"][0]
    with pytest.raises(ValueError, match="capability_revoked"):
        service.context(
            {
                "fanout_id": plan["fanout_id"],
                "task_id": task["task_id"],
                "capability_token": task["capability_packet"]["token"],
            }
        )


def test_unprepared_source_requires_transcription_approval(tmp_path: Path) -> None:
    source = tmp_path / "new.mp4"
    source.write_bytes(b"new-video")
    result = GovernedFanoutService(tmp_path).plan(
        {
            "source_path": str(source),
            "source_sha256": sha256(source.read_bytes()).hexdigest(),
        }
    )
    assert result["status"] == "TRANSCRIPT_REQUIRED"
    assert result["requires_creator_approval"] is True


def test_creator_review_actions_never_imply_publish_ready(tmp_path: Path) -> None:
    event = record_review_action(
        tmp_path,
        {
            "action": "approve",
            "output_id": "short-1",
            "creator_statement": "Approve this creator-review draft only.",
        },
    )
    assert event["resulting_state"] == "CREATOR_REVIEW"
    assert event["publish_ready"] is False
    assert inspect_review_actions(tmp_path)[0]["event_id"] == event["event_id"]
