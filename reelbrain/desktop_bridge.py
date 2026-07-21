"""JSON-over-stdio bridge used by the Tauri desktop host."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback

from .desktop_state import DesktopMemoryService, inspect_review_actions, record_review_action
from .fanout import FanoutError, GovernedFanoutService
from .toolbox import ToolManifest, ToolboxManager, sha256_file


def dispatch(command: str, payload: dict[str, object], workspace: Path) -> object:
    fanout = GovernedFanoutService(workspace)
    creator_id = str(payload.get("creator_id") or "creator-founder")
    if command == "memory_inspect":
        return DesktopMemoryService(workspace, creator_id).inspect()
    if command == "memory_mutate":
        return DesktopMemoryService(workspace, creator_id).mutate(payload)
    if command == "fanout_plan":
        return fanout.plan(payload)
    if command == "fanout_context":
        return fanout.context(payload)
    if command == "fanout_submit":
        return fanout.submit(payload)
    if command == "fanout_steer":
        return fanout.steer(payload)
    if command == "fanout_evidence":
        evidence = fanout.evidence(int(payload.get("limit") or 100))
        evidence["review_events"] = inspect_review_actions(workspace)
        return evidence
    if command == "fanout_verify":
        return fanout.verify_evidence(str(payload.get("fanout_id") or ""))
    if command == "review_action":
        return record_review_action(workspace, payload)
    if command == "tool_stage_generated":
        artifact = Path(str(payload.get("artifact_path") or "")).expanduser().resolve()
        approval_id = str(payload.get("approval_id") or "").strip()
        tool_id = str(payload.get("tool_id") or "").strip()
        capabilities = tuple(str(item) for item in payload.get("capabilities") or ())
        dependencies = tuple(str(item) for item in payload.get("dependencies") or ())
        if not approval_id or not tool_id or not capabilities:
            raise ValueError("tool_stage_fields_required")
        manifest = ToolManifest(
            tool_id=tool_id,
            version="0.1.0",
            digest=sha256_file(artifact),
            origin="generated",
            entrypoint="tool.py",
            capabilities=capabilities,
            dependencies=dependencies,
        )
        record = ToolboxManager().stage_generated(approval_id, artifact, manifest)
        return {
            "status": "quarantined",
            "tool_id": record.manifest.tool_id,
            "digest": record.manifest.digest,
            "artifact_path": str(record.artifact_path),
            "manifest_path": str(record.manifest_path),
        }
    if command == "tool_deploy_approved":
        approval_id = str(payload.get("approval_id") or "").strip()
        receipt = str(payload.get("approval_receipt_id") or "").strip()
        report = payload.get("auditor_report")
        if not approval_id or not receipt or not isinstance(report, dict):
            raise ValueError("tool_deploy_fields_required")
        if report.get("passed") is True and report.get("test_command") != (
            "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py"
        ):
            raise ValueError("tool_auditor_test_evidence_required")
        record = ToolboxManager().approve_custom(
            approval_id,
            human_approver_id="human:creator-founder",
            approval_receipt_id=receipt,
            auditor_report=report,
        )
        return {
            "status": "deployed",
            "tool_id": record.manifest.tool_id,
            "digest": record.manifest.digest,
            "artifact_path": str(record.artifact_path),
            "manifest_path": str(record.manifest_path),
        }
    raise ValueError("desktop_bridge_command_unsupported")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(json.dumps({"ok": False, "error": {"code": "command_required"}}))
        return 2
    command = arguments[0]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload_must_be_object")
        workspace = Path(str(payload.pop("workspace", Path.cwd()))).expanduser().resolve()
        result = dispatch(command, payload, workspace)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except (FanoutError, ValueError, KeyError, PermissionError) as error:
        code = getattr(error, "code", str(error).strip("'")) or "desktop_bridge_error"
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(error)}}))
        return 1
    except Exception as error:  # pragma: no cover - defensive process boundary
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "desktop_bridge_internal_error",
                        "message": str(error),
                        "trace": traceback.format_exc(limit=3),
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
