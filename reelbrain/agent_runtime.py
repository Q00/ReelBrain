"""Interruptible provider-neutral agent-team orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .lifecycle import RunLedger
from .toolbox import ToolManifest, ToolRecord, ToolboxManager

AgentName = Literal[
    "meaning-scout",
    "hook-scout",
    "creator-advocate",
    "context-guardian",
    "showrunner",
    "assembler",
    "thumbnail-designer",
    "verifier",
    "toolsmith",
    "tool-auditor",
    "sleep",
]

AGENTS: tuple[AgentName, ...] = (
    "meaning-scout",
    "hook-scout",
    "creator-advocate",
    "context-guardian",
    "showrunner",
    "assembler",
    "thumbnail-designer",
    "verifier",
    "toolsmith",
    "tool-auditor",
    "sleep",
)


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    agent: AgentName
    task_type: str
    payload: dict[str, object]
    epoch: int


@dataclass(frozen=True)
class SteeringEvent:
    steering_id: str
    message: str
    target_agent: AgentName | None
    previous_epoch: int
    current_epoch: int


@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    requesting_agent: AgentName
    description: str
    capabilities: tuple[str, ...]
    status: str
    equivalent_tool_id: str | None = None


@dataclass
class AgentTeamRuntime:
    project_id: str
    creator_id: str
    toolbox: ToolboxManager
    ledger: RunLedger = field(init=False)
    tasks: dict[str, AgentTask] = field(default_factory=dict)
    results: dict[str, object] = field(default_factory=dict)
    steering_events: list[SteeringEvent] = field(default_factory=list)
    tool_requests: dict[str, ToolRequest] = field(default_factory=dict)
    cancelled: bool = False

    def __post_init__(self) -> None:
        self.ledger = RunLedger.create(
            project_id=self.project_id, creator_id=self.creator_id
        )

    def submit_task(
        self, *, agent: AgentName, task_type: str, payload: dict[str, object]
    ) -> AgentTask:
        self._require_agent(agent)
        if self.cancelled:
            raise RuntimeError("agent_team_cancelled")
        task = AgentTask(
            task_id=f"task_{uuid4().hex}",
            agent=agent,
            task_type=task_type,
            payload=dict(payload),
            epoch=self.ledger.epoch,
        )
        self.tasks[task.task_id] = task
        return task

    def complete_task(self, task: AgentTask, result: object) -> None:
        if self.cancelled:
            raise RuntimeError("agent_team_cancelled")
        if self.tasks.get(task.task_id) != task:
            raise ValueError("unknown_agent_task")
        self.ledger.assert_epoch(task.epoch)
        self.results[task.task_id] = result

    def steer(self, message: str, *, target_agent: AgentName | None = None) -> SteeringEvent:
        if not message.strip():
            raise ValueError("steering_message_required")
        if target_agent is not None:
            self._require_agent(target_agent)
        previous = self.ledger.epoch
        self.ledger.interrupt(reason=f"creator_steering:{message}")
        event = SteeringEvent(
            steering_id=f"steering_{uuid4().hex}",
            message=message,
            target_agent=target_agent,
            previous_epoch=previous,
            current_epoch=self.ledger.epoch,
        )
        self.steering_events.append(event)
        return event

    def cancel(self, reason: str) -> None:
        if self.cancelled:
            return
        self.ledger.interrupt(reason=f"creator_cancelled:{reason}")
        self.cancelled = True

    def request_tool(
        self,
        *,
        agent: AgentName,
        description: str,
        capabilities: tuple[str, ...],
    ) -> ToolRequest:
        self._require_agent(agent)
        if not capabilities:
            raise ValueError("tool_capabilities_required")
        equivalent = self.toolbox.find_equivalent(capabilities)
        request = ToolRequest(
            request_id=f"tool_request_{uuid4().hex}",
            requesting_agent=agent,
            description=description,
            capabilities=capabilities,
            status="reuse_approved_tool" if equivalent else "toolsmith_required",
            equivalent_tool_id=equivalent.manifest.tool_id if equivalent else None,
        )
        self.tool_requests[request.request_id] = request
        return request

    def toolsmith_stage(
        self,
        request_id: str,
        *,
        acting_agent: AgentName,
        artifact: Path | str,
        manifest: ToolManifest,
    ) -> ToolRecord:
        if acting_agent != "toolsmith":
            raise PermissionError("only_toolsmith_can_stage_tools")
        request = self._require_tool_request(request_id)
        if request.status != "toolsmith_required":
            raise ValueError("tool_request_does_not_require_generation")
        if tuple(manifest.capabilities) != request.capabilities:
            raise ValueError("generated_tool_capability_mismatch")
        record = self.toolbox.stage_generated(request_id, artifact, manifest)
        self.tool_requests[request_id] = ToolRequest(
            **{**request.__dict__, "status": "quarantined"}
        )
        return record

    def human_approve_tool(
        self,
        request_id: str,
        *,
        human_approver_id: str,
        approval_receipt_id: str,
        auditor_report: dict[str, object],
    ) -> ToolRecord:
        request = self._require_tool_request(request_id)
        if request.status != "quarantined":
            raise ValueError("tool_not_quarantined")
        record = self.toolbox.approve_custom(
            request_id,
            human_approver_id=human_approver_id,
            approval_receipt_id=approval_receipt_id,
            auditor_report=auditor_report,
        )
        self.tool_requests[request_id] = ToolRequest(
            **{**request.__dict__, "status": "approved"}
        )
        return record

    @staticmethod
    def _require_agent(agent: str) -> None:
        if agent not in AGENTS:
            raise ValueError("unknown_reelbrain_agent")

    def _require_tool_request(self, request_id: str) -> ToolRequest:
        try:
            return self.tool_requests[request_id]
        except KeyError as exc:
            raise KeyError("tool_request_not_found") from exc
