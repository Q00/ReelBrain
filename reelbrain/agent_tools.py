"""Agent-owned semantic tools for ReelBrain execution.

The agent is the actor.  A Python package, provider SDK, local binary, or remote
adapter is only an implementation detail behind a semantic tool contract.  This
keeps the public execution trace stable when an implementation changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Callable, Literal, Mapping, TypeVar

from .agent_runtime import AgentName, AgentTeamRuntime
from .runtime_guard import RuntimeGuard
from .toolbox import ToolboxManager


T = TypeVar("T")
GovernanceOwner = Literal["agent-runtime", "bound-implementation"]


@dataclass(frozen=True)
class AgentToolContract:
    """Portable description of one tool exposed to a ReelBrain agent."""

    tool_id: str
    capability: str
    description: str
    allowed_agents: tuple[AgentName, ...]
    input_schema: Mapping[str, object]
    data_effects: tuple[str, ...]
    implementation_dependencies: tuple[str, ...] = ()
    governance_owner: GovernanceOwner = "agent-runtime"


@dataclass(frozen=True)
class AgentToolCall:
    task_id: str
    agent_id: AgentName
    tool_id: str
    capability: str
    status: str
    input_keys: tuple[str, ...]
    implementation_dependencies: tuple[str, ...]
    governance_owner: GovernanceOwner


DEFAULT_DOGFOOD_TOOL_CONTRACTS: tuple[AgentToolContract, ...] = (
    AgentToolContract(
        tool_id="analyze-story-structure",
        capability="editorial:analyze-story",
        description="Score self-contained educational arcs, complete thoughts, and payoff structure.",
        allowed_agents=("meaning-scout",),
        input_schema={"type": "object", "required": ["candidate_ids", "transcript_path"]},
        data_effects=("read transcript", "write story assessment"),
    ),
    AgentToolContract(
        tool_id="analyze-retention",
        capability="editorial:analyze-retention",
        description="Assess hook strength, pacing, dead space, and payoff timing without clickbait.",
        allowed_agents=("hook-scout",),
        input_schema={"type": "object", "required": ["candidate_ids", "transcript_path"]},
        data_effects=("read transcript", "write retention assessment"),
    ),
    AgentToolContract(
        tool_id="apply-creator-taste",
        capability="editorial:apply-taste",
        description="Apply active creator-approved behavioral priors to captions, framing, and visual rhythm.",
        allowed_agents=("creator-advocate",),
        input_schema={"type": "object", "required": ["candidate_ids", "preference_ids"]},
        data_effects=("read approved memory", "write style assessment"),
    ),
    AgentToolContract(
        tool_id="validate-context-continuity",
        capability="editorial:validate-context",
        description="Detect missing caveats, distorted claims, and cuts that end before a thought is complete.",
        allowed_agents=("context-guardian",),
        input_schema={"type": "object", "required": ["candidate_ids", "transcript_path"]},
        data_effects=("read transcript", "write continuity assessment"),
    ),
    AgentToolContract(
        tool_id="transcribe-bilingual",
        capability="transcript:build-bilingual",
        description=(
            "Build timestamped Korean and English transcript tracks from one source. "
            "The bound implementation may use FFmpeg and an approved STT provider."
        ),
        allowed_agents=("meaning-scout",),
        input_schema={
            "type": "object",
            "required": ["source_id", "source_path", "provider_call_id"],
        },
        data_effects=("read creator video", "write transcript", "provider transmission"),
        governance_owner="bound-implementation",
    ),
    AgentToolContract(
        tool_id="plan-editorial-candidates",
        capability="editorial:plan",
        description=(
            "Fan out bounded editorial perspectives and synthesize grounded Shorts and "
            "long-form candidates without rendering or publishing."
        ),
        allowed_agents=("showrunner",),
        input_schema={
            "type": "object",
            "required": ["source_id", "transcript_path", "short_count"],
        },
        data_effects=("read transcript", "write editorial plan", "provider transmission"),
        governance_owner="bound-implementation",
    ),
    AgentToolContract(
        tool_id="render-vertical-short",
        capability="media:render-short",
        description=(
            "Render one approved 30-60 second vertical Short with centered source, "
            "blurred background, title, and bilingual caption overlays."
        ),
        allowed_agents=("hook-scout", "assembler"),
        input_schema={
            "type": "object",
            "required": ["source_id", "output_id", "start_seconds", "duration_seconds"],
        },
        data_effects=("read creator video", "write video", "write subtitle sidecars"),
        implementation_dependencies=("ffmpeg", "ffprobe", "pillow>=12.2,<13"),
    ),
    AgentToolContract(
        tool_id="render-long-form",
        capability="media:render-long",
        description=(
            "Render one approved 10-15 minute long-form cut with a natural ending and "
            "bilingual caption deliverables."
        ),
        allowed_agents=("context-guardian", "assembler"),
        input_schema={
            "type": "object",
            "required": ["source_id", "output_id", "segments"],
        },
        data_effects=("read creator video", "write video", "write subtitle sidecars"),
        implementation_dependencies=("ffmpeg", "ffprobe", "pillow>=12.2,<13"),
    ),
    AgentToolContract(
        tool_id="overlay-timed-image",
        capability="media:overlay-image",
        description=(
            "Place a creator-supplied image over an approved video between exact start "
            "and end timestamps, with bounded position, scale, opacity, and transition settings."
        ),
        allowed_agents=("creator-advocate", "assembler"),
        input_schema={
            "type": "object",
            "required": [
                "source_id",
                "output_id",
                "image_path",
                "start_seconds",
                "end_seconds",
            ],
        },
        data_effects=("read creator video", "read creator image", "write revised video"),
        implementation_dependencies=("ffmpeg", "ffprobe"),
    ),
    AgentToolContract(
        tool_id="design-thumbnail",
        capability="thumbnail:design",
        description=(
            "Generate a creator-authorized hook-matched background and compose one "
            "readable thumbnail for the selected output."
        ),
        allowed_agents=("creator-advocate", "thumbnail-designer"),
        input_schema={
            "type": "object",
            "required": ["source_id", "output_id", "title", "orientation"],
        },
        data_effects=(
            "read transcript excerpt",
            "write generated image",
            "provider transmission",
        ),
        implementation_dependencies=("pillow>=12.2,<13",),
        governance_owner="bound-implementation",
    ),
)


class AgentToolExecutor:
    """Dispatch semantic tools on behalf of named agents and retain evidence."""

    def __init__(
        self,
        *,
        project_id: str,
        creator_id: str,
        workspace_root: Path | str,
        read_roots: tuple[Path | str, ...] = (),
        toolbox: ToolboxManager | None = None,
        contracts: tuple[AgentToolContract, ...] = DEFAULT_DOGFOOD_TOOL_CONTRACTS,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.read_roots = tuple(read_roots)
        self.toolbox = toolbox or ToolboxManager()
        self.team = AgentTeamRuntime(
            project_id=project_id,
            creator_id=creator_id,
            toolbox=self.toolbox,
        )
        self.contracts = {contract.tool_id: contract for contract in contracts}
        if len(self.contracts) != len(contracts):
            raise ValueError("duplicate_agent_tool_contract")
        self.calls: list[AgentToolCall] = []

    def write_execution_contract(self, destination: Path | str) -> Path:
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "profile": "reelbrain.dev/agent-tool-contract/v1alpha1",
                    "principle": "agents invoke semantic tools; packages are bound implementations",
                    "project_id": self.team.project_id,
                    "creator_id": self.team.creator_id,
                    "tools": [asdict(contract) for contract in self.contracts.values()],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def invoke(
        self,
        *,
        agent: AgentName,
        tool_id: str,
        payload: Mapping[str, object],
        dispatch: Callable[[], T],
    ) -> T:
        try:
            contract = self.contracts[tool_id]
        except KeyError as exc:
            raise KeyError("unknown_agent_tool") from exc
        if agent not in contract.allowed_agents:
            raise PermissionError("agent_not_allowed_to_invoke_tool")
        task = self.team.submit_task(
            agent=agent,
            task_type=contract.capability,
            payload=dict(payload),
        )
        try:
            if contract.governance_owner == "agent-runtime":
                guard = RuntimeGuard(
                    workspace_root=self.workspace_root,
                    local_allowlist=self.read_roots,
                    project_id=self.team.project_id,
                    creator_id=self.team.creator_id,
                    agent_id=agent,
                    tool_names=(),
                    toolbox=self.toolbox,
                )
                result = guard.run_callback_tool(
                    tool_id=contract.tool_id,
                    capability=contract.capability,
                    dispatch=dispatch,
                    official=True,
                    tool_description=contract.description,
                    input_schema=contract.input_schema,
                    data_effects=contract.data_effects,
                    implementation_dependencies=contract.implementation_dependencies,
                )
            else:
                # Provider-aware implementations already perform the narrower ACP,
                # consent, secret, destination, and spend checks at each effect.
                result = dispatch()
            self.team.complete_task(task, result)
        except Exception:
            self.calls.append(
                AgentToolCall(
                    task_id=task.task_id,
                    agent_id=agent,
                    tool_id=contract.tool_id,
                    capability=contract.capability,
                    status="failed",
                    input_keys=tuple(sorted(payload)),
                    implementation_dependencies=contract.implementation_dependencies,
                    governance_owner=contract.governance_owner,
                )
            )
            raise
        self.calls.append(
            AgentToolCall(
                task_id=task.task_id,
                agent_id=agent,
                tool_id=contract.tool_id,
                capability=contract.capability,
                status="completed_claim_pending_independent_verification",
                input_keys=tuple(sorted(payload)),
                implementation_dependencies=contract.implementation_dependencies,
                governance_owner=contract.governance_owner,
            )
        )
        return result

    def write_trace(self, destination: Path | str) -> Path:
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        prior_calls: list[dict[str, object]] = []
        if path.is_file():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(prior, dict) and isinstance(prior.get("calls"), list):
                    prior_calls = [
                        dict(call) for call in prior["calls"] if isinstance(call, dict)
                    ]
            except (OSError, json.JSONDecodeError):
                prior_calls = []
        merged: dict[str, dict[str, object]] = {
            str(call.get("task_id")): call for call in prior_calls if call.get("task_id")
        }
        for call in self.calls:
            document = asdict(call)
            merged[call.task_id] = document
        path.write_text(
            json.dumps(
                {
                    "profile": "reelbrain.dev/agent-tool-trace/v1alpha1",
                    "claim_is_not_confirmation": True,
                    "calls": list(merged.values()),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path
