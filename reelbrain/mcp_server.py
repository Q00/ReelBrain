"""Official MCP stdio adapter for ReelBrain's governed desktop services.

The adapter contains no editorial trust policy. It translates closed MCP tool
calls into the same durable services used by ReelBrain Desktop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .desktop_state import DesktopMemoryService, inspect_review_actions, record_review_action
from .fanout import GovernedFanoutService


mcp = FastMCP(
    "ReelBrain",
    instructions=(
        "ReelBrain owns candidate truth, capability grants, creator taste, evidence, "
        "and effect authorization. Memory is a behavioral prior, never evidence. "
        "Never invent candidate IDs or claim render/publish effects without the "
        "corresponding ReelBrain authority."
    ),
)


def _workspace(value: str | None) -> Path:
    return Path(value or Path.cwd()).expanduser().resolve()


@mcp.tool(
    name="reelbrain_plan_fanout",
    description=(
        "Create one governed four-persona editorial plan from a creator-owned local "
        "source. Returns unique least-privilege capability packets, canonical "
        "candidate and memory snapshot digests, and root submission/steering "
        "authority. If no canonical transcript/catalog exists, returns "
        "TRANSCRIPT_REQUIRED without starting agents or authorizing provider spend."
    ),
)
def plan_fanout(
    source_path: str,
    source_sha256: str,
    project_id: str = "desktop-project",
    creator_id: str = "creator-founder",
    current_steering: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    return GovernedFanoutService(_workspace(workspace)).plan(
        {
            "source_path": source_path,
            "source_sha256": source_sha256,
            "project_id": project_id,
            "creator_id": creator_id,
            "current_steering": current_steering,
        }
    )


@mcp.tool(
    name="reelbrain_get_task_context",
    description=(
        "Use a persona capability packet to read only its authorized canonical "
        "candidate IDs and filtered creator-approved behavioral priors. Records an "
        "allow or denial receipt before returning. Never returns raw source paths, "
        "secrets, provider credentials, or memory write authority."
    ),
)
def get_task_context(
    fanout_id: str,
    task_id: str,
    capability_token: str,
    candidate_ids: list[str] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    request: dict[str, object] = {
        "fanout_id": fanout_id,
        "task_id": task_id,
        "capability_token": capability_token,
    }
    if candidate_ids is not None:
        request["candidate_ids"] = candidate_ids
    return GovernedFanoutService(_workspace(workspace)).context(request)


@mcp.tool(
    name="reelbrain_submit_fanout",
    description=(
        "Submit exactly four persona result envelopes for grounded validation. "
        "Rejects unknown candidate IDs, stale workflow epochs, stale catalog or "
        "memory snapshots, and mismatched task/persona sets. On success persists an "
        "accepted editorial-plan digest but does not render or publish."
    ),
)
def submit_fanout(
    fanout_id: str,
    root_capability_token: str,
    results: list[dict[str, Any]],
    expected_evidence_revision: int | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    request: dict[str, object] = {
        "fanout_id": fanout_id,
        "root_capability_token": root_capability_token,
        "results": results,
    }
    if expected_evidence_revision is not None:
        request["expected_evidence_revision"] = expected_evidence_revision
    return GovernedFanoutService(_workspace(workspace)).submit(request)


@mcp.tool(
    name="reelbrain_steer_fanout",
    description=(
        "Apply explicit creator steering or cancellation to a governed fan-out. "
        "Advances the workflow epoch, revokes previous grants, records evidence, and "
        "requires a new plan before more persona work can be accepted."
    ),
)
def steer_fanout(
    fanout_id: str,
    root_capability_token: str,
    message: str,
    action: str = "steer",
    workspace: str | None = None,
) -> dict[str, Any]:
    return GovernedFanoutService(_workspace(workspace)).steer(
        {
            "fanout_id": fanout_id,
            "root_capability_token": root_capability_token,
            "message": message,
            "action": action,
        }
    )


@mcp.tool(
    name="reelbrain_inspect_creator_memory",
    description=(
        "Inspect active/disabled preferences, proposals, versions, provenance, and "
        "content-free deletion tombstones for one creator. This is a read-only "
        "behavior-prior view and must never be used as source evidence."
    ),
)
def inspect_creator_memory(
    creator_id: str = "creator-founder", workspace: str | None = None
) -> dict[str, Any]:
    return DesktopMemoryService(_workspace(workspace), creator_id).inspect()


@mcp.tool(
    name="reelbrain_record_creator_feedback",
    description=(
        "Record an authenticated creator memory action: episode, remember, confirm, "
        "edit, disable, enable, or delete. Requires the expected memory revision and "
        "an explicit creator statement. Ordinary episode feedback does not become a "
        "durable preference without remember or confirmation."
    ),
)
def record_creator_feedback(
    action: str,
    expected_revision: int,
    creator_statement: str,
    creator_id: str = "creator-founder",
    project_id: str = "desktop-project",
    category: str | None = None,
    value: str | None = None,
    scope: dict[str, str | None] | None = None,
    preference_id: str | None = None,
    proposal_id: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    request: dict[str, object] = {
        "action": action,
        "expected_revision": expected_revision,
        "creator_statement": creator_statement,
        "creator_id": creator_id,
        "project_id": project_id,
    }
    for key, item in {
        "category": category,
        "value": value,
        "scope": scope,
        "preference_id": preference_id,
        "proposal_id": proposal_id,
    }.items():
        if item is not None:
            request[key] = item
    return DesktopMemoryService(_workspace(workspace), creator_id).mutate(request)


@mcp.tool(
    name="reelbrain_inspect_evidence",
    description=(
        "Read redacted hash-linked governance events, fan-out projections, denials, "
        "and creator-review decisions. Raw capability tokens and credentials are "
        "never returned."
    ),
)
def inspect_evidence(limit: int = 100, workspace: str | None = None) -> dict[str, Any]:
    root = _workspace(workspace)
    result = GovernedFanoutService(root).evidence(limit)
    result["review_events"] = inspect_review_actions(root)
    return result


@mcp.tool(
    name="reelbrain_record_review_action",
    description=(
        "Record an explicit creator approve/reject/revise decision for an existing "
        "draft. The resulting state remains CREATOR_REVIEW and publish_ready remains "
        "false; this tool never publishes."
    ),
)
def record_review(
    action: str,
    output_id: str,
    creator_statement: str,
    creator_id: str = "creator-founder",
    project_id: str = "desktop-project",
    workspace: str | None = None,
) -> dict[str, Any]:
    return record_review_action(
        _workspace(workspace),
        {
            "action": action,
            "output_id": output_id,
            "creator_statement": creator_statement,
            "creator_id": creator_id,
            "project_id": project_id,
        },
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
