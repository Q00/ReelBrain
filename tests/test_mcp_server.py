from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from reelbrain.mcp_server import mcp


def test_mcp_exposes_governed_reelbrain_tools_with_truthful_descriptions() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    required = {
        "reelbrain_plan_fanout",
        "reelbrain_get_task_context",
        "reelbrain_submit_fanout",
        "reelbrain_steer_fanout",
        "reelbrain_inspect_creator_memory",
        "reelbrain_record_creator_feedback",
        "reelbrain_inspect_evidence",
        "reelbrain_record_review_action",
    }
    assert required <= set(by_name)
    assert "does not render or publish" in by_name["reelbrain_submit_fanout"].description
    assert "behavioral priors" in by_name["reelbrain_get_task_context"].description
    assert "never publishes" in by_name["reelbrain_record_review_action"].description
