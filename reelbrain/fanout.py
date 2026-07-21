"""Governed, host-driven editorial fan-out for ReelBrain Desktop."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from typing import Iterator
from uuid import uuid4

import fcntl

from .desktop_state import DesktopMemoryService, atomic_write_json, utc_now


PROTOCOL = "reelbrain.dev/governed-fanout/v1alpha1"
PERSONAS = (
    "meaning-scout",
    "hook-scout",
    "creator-advocate",
    "context-guardian",
)

PERSONA_INSTRUCTIONS = {
    "meaning-scout": (
        "Act as the Story Editor. Choose self-contained educational arcs with a clear "
        "setup, explanation, and payoff. Prefer natural edit boundaries and complete thoughts."
    ),
    "hook-scout": (
        "Act as the Retention Editor. Rank openings, pacing opportunities, and payoffs that "
        "hold attention without sensationalism or unsupported claims."
    ),
    "creator-advocate": (
        "Act as the Style Editor. Apply only supplied creator-approved behavioral priors "
        "when judging caption rhythm, framing potential, visual emphasis, and voice."
    ),
    "context-guardian": (
        "Act as the Continuity Editor. Reject cuts that remove caveats, distort meaning, "
        "depend on missing context, or end before the speaker completes the thought."
    ),
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return f"sha256:{sha256(canonical_json(value)).hexdigest()}"


def token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FanoutError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class GovernedFanoutService:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.root = self.workspace / ".reelbrain" / "desktop" / "fanout"
        self.root.mkdir(parents=True, exist_ok=True)

    def _fanout_root(self, fanout_id: str) -> Path:
        if not fanout_id.startswith("fanout_") or "/" in fanout_id:
            raise FanoutError("fanout_id_invalid")
        return self.root / fanout_id

    def _projection(self, fanout_id: str) -> dict[str, object]:
        path = self._fanout_root(fanout_id) / "evidence-record.json"
        if not path.is_file():
            raise FanoutError("fanout_not_found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _capabilities(self, fanout_id: str) -> dict[str, object]:
        path = self._fanout_root(fanout_id) / "capabilities.runtime.json"
        if not path.is_file():
            raise FanoutError("capability_registry_missing")
        return json.loads(path.read_text(encoding="utf-8"))

    def _append_event(
        self,
        fanout_id: str,
        *,
        event_type: str,
        actor: str,
        decision: str,
        reason_code: str,
        details: dict[str, object] | None = None,
        task_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict[str, object]:
        root = self._fanout_root(fanout_id)
        projection_path = root / "evidence-record.json"
        events_path = root / "evidence-events.jsonl"
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        sequence = int(projection.get("last_event_sequence", 0)) + 1
        previous = str(projection.get("last_event_hash") or "sha256:genesis")
        event = {
            "sequence": sequence,
            "event_id": f"event_{uuid4().hex}",
            "event_type": event_type,
            "fanout_id": fanout_id,
            "project_id": projection["project_id"],
            "creator_id": projection["creator_id"],
            "task_id": task_id,
            "grant_id": grant_id,
            "epoch": projection["epoch"],
            "actor": actor,
            "decision": decision,
            "reason_code": reason_code,
            "receipt_id": f"receipt_{uuid4().hex}",
            "details": details or {},
            "previous_event_hash": previous,
            "created_at": utc_now(),
        }
        event["event_hash"] = digest(event)
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        projection["revision"] = int(projection.get("revision", 0)) + 1
        projection["last_event_sequence"] = sequence
        projection["last_event_hash"] = event["event_hash"]
        if decision == "deny":
            projection["denial_count"] = int(projection.get("denial_count", 0)) + 1
        atomic_write_json(projection_path, projection)
        return event

    def _find_catalog(self, source_sha256: str) -> dict[str, object] | None:
        dogfood = self.workspace / ".reelbrain" / "dogfood"
        inventories = sorted(dogfood.glob("*/run/source_inventory.json"), reverse=True)
        for inventory_path in inventories:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            source = next(
                (item for item in inventory if item.get("sha256") == source_sha256), None
            )
            if source is None:
                continue
            run_root = inventory_path.parent
            source_id = str(source["source_id"])
            editorial_path = run_root / source_id / "editorial_plan.json"
            transcript_path = run_root / source_id / "bilingual_transcript.json"
            if not editorial_path.is_file() or not transcript_path.is_file():
                return None
            editorial = json.loads(editorial_path.read_text(encoding="utf-8"))
            trace = editorial.get("trace") or []
            candidates: list[dict[str, object]] = []
            for item in trace:
                payload = item.get("request_payload") if isinstance(item, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("short_candidates"), list):
                    candidates = payload["short_candidates"]
                    break
            if not candidates:
                candidates = list(editorial.get("shorts") or [])
            unique: dict[str, dict[str, object]] = {}
            for candidate in candidates:
                candidate_id = str(candidate.get("candidate_id") or "")
                if not candidate_id or candidate_id in unique:
                    continue
                unique[candidate_id] = {
                    "candidate_id": candidate_id,
                    "start_chunk_id": candidate.get("start_chunk_id")
                    or (candidate.get("chunk_ids") or [None])[0],
                    "end_chunk_id": candidate.get("end_chunk_id")
                    or (candidate.get("chunk_ids") or [None])[-1],
                    "start_seconds": candidate.get("start_seconds"),
                    "end_seconds": candidate.get("end_seconds"),
                    "duration_seconds": candidate.get("duration_seconds")
                    or float(candidate.get("end_seconds", 0))
                    - float(candidate.get("start_seconds", 0)),
                    "text": candidate.get("text", ""),
                    "confidence": candidate.get("confidence"),
                }
            return {
                "source": source,
                "source_id": source_id,
                "transcript_path": str(transcript_path),
                "transcript_sha256": sha256(transcript_path.read_bytes()).hexdigest(),
                "candidates": list(unique.values())[:40],
                "long_form": editorial.get("long_form"),
                "run_root": str(run_root),
            }
        return None

    def plan(self, request: dict[str, object]) -> dict[str, object]:
        source_path = Path(str(request.get("source_path") or "")).expanduser().resolve()
        source_sha256 = str(request.get("source_sha256") or "").strip()
        creator_id = str(request.get("creator_id") or "creator-founder")
        project_id = str(request.get("project_id") or "desktop-project")
        if not source_path.is_file() or len(source_sha256) != 64:
            raise FanoutError("source_snapshot_invalid")
        if file_sha256(source_path) != source_sha256:
            raise FanoutError("source_digest_mismatch")
        catalog = self._find_catalog(source_sha256)
        if catalog is None:
            return {
                "status": "TRANSCRIPT_REQUIRED",
                "source_path": str(source_path),
                "source_sha256": source_sha256,
                "requires_creator_approval": True,
                "required_effect": "transcribe_and_build_candidate_catalog",
                "message": "This source has no canonical transcript/catalog yet. Approve transcription before starting the editorial agents.",
            }
        memory = DesktopMemoryService(self.workspace, creator_id).inspect()
        current_steering = str(request.get("current_steering") or "").strip() or None
        memory_snapshot = {
            "creator_id": creator_id,
            "revision": memory["revision"],
            "preferences": [
                item for item in memory["preferences"] if item["status"] == "active"
            ],
            "current_steering": current_steering,
            "principle": memory["principle"],
        }
        candidates = catalog["candidates"]
        if not candidates:
            raise FanoutError("candidate_catalog_empty")
        fanout_id = f"fanout_{uuid4().hex}"
        root = self._fanout_root(fanout_id)
        root.mkdir(parents=True, exist_ok=False)
        catalog_digest = digest(candidates)
        memory_digest = digest(memory_snapshot)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        capability_records: list[dict[str, object]] = []
        tasks: list[dict[str, object]] = []
        allowed_ids = [str(item["candidate_id"]) for item in candidates]
        for persona in PERSONAS:
            token = secrets.token_urlsafe(32)
            grant_id = f"grant_{uuid4().hex}"
            task_id = f"task_{persona.replace('-', '_')}_{uuid4().hex[:8]}"
            record = {
                "grant_id": grant_id,
                "token_hash": token_hash(token),
                "kind": "persona",
                "persona": persona,
                "task_id": task_id,
                "allowed_tools": ["reelbrain_get_task_context"],
                "allowed_candidate_ids": allowed_ids,
                "memory_read_categories": [
                    str(item["category"]) for item in memory_snapshot["preferences"]
                ],
                "epoch": 1,
                "expires_at": expires_at,
                "max_calls": 3,
                "call_count": 0,
                "revoked": False,
            }
            capability_records.append(record)
            tasks.append(
                {
                    "task_id": task_id,
                    "fanout_id": fanout_id,
                    "persona": persona,
                    "epoch": 1,
                    "snapshot_digest": catalog_digest,
                    "memory_snapshot_digest": memory_digest,
                    "instruction": PERSONA_INSTRUCTIONS[persona],
                    "inline_candidate_summaries": candidates,
                    "capability_packet": {
                        "grant_id": grant_id,
                        "token": token,
                        "expires_at": expires_at,
                        "max_calls": 3,
                        "max_request_bytes": 65536,
                    },
                }
            )
        root_token = secrets.token_urlsafe(32)
        root_grant_id = f"grant_root_{uuid4().hex}"
        capability_records.append(
            {
                "grant_id": root_grant_id,
                "token_hash": token_hash(root_token),
                "kind": "root",
                "allowed_tools": [
                    "reelbrain_submit_fanout",
                    "reelbrain_steer_fanout",
                    "reelbrain_cancel_fanout",
                ],
                "epoch": 1,
                "expires_at": expires_at,
                "max_calls": 8,
                "call_count": 0,
                "revoked": False,
            }
        )
        source_snapshot = {
            "source_path": str(source_path),
            "source_sha256": source_sha256,
            "source_id": catalog["source_id"],
            "transcript_path": catalog["transcript_path"],
            "transcript_sha256": catalog["transcript_sha256"],
        }
        projection = {
            "protocol_version": PROTOCOL,
            "fanout_id": fanout_id,
            "project_id": project_id,
            "creator_id": creator_id,
            "evidence_state": "PLANNED",
            "epoch": 1,
            "source_sha256": source_sha256,
            "transcript_sha256": catalog["transcript_sha256"],
            "catalog_sha256": catalog_digest,
            "memory_snapshot_digest": memory_digest,
            "issued_grant_ids": [item["grant_id"] for item in capability_records],
            "provider_spend_cents": 0,
            "denial_count": 0,
            "accepted_submission_sha256": None,
            "editorial_plan_sha256": None,
            "revision": 0,
            "last_event_sequence": 0,
            "last_event_hash": "sha256:genesis",
            "created_at": utc_now(),
            "expires_at": expires_at,
        }
        atomic_write_json(root / "source-snapshot.json", source_snapshot)
        atomic_write_json(root / "candidate-catalog.json", candidates)
        atomic_write_json(root / "memory-snapshot.json", memory_snapshot)
        atomic_write_json(root / "evidence-record.json", projection)
        atomic_write_json(root / "capabilities.runtime.json", {"grants": capability_records})
        atomic_write_json(
            root / "capability-grants.redacted.json",
            {
                "grants": [
                    {key: value for key, value in item.items() if key != "token_hash"}
                    for item in capability_records
                ]
            },
        )
        (root / "evidence-events.jsonl").touch()
        self._append_event(
            fanout_id,
            event_type="fanout_planned",
            actor="reelbrain-desktop",
            decision="allow",
            reason_code="governed_fanout_plan_issued",
            details={"task_ids": [item["task_id"] for item in tasks]},
            grant_id=root_grant_id,
        )
        projection = self._projection(fanout_id)
        return {
            "status": "READY_FOR_HOST_DISPATCH",
            "protocol": PROTOCOL,
            "fanout_id": fanout_id,
            "project_id": project_id,
            "creator_id": creator_id,
            "epoch": 1,
            "evidence_revision": projection["revision"],
            "source_sha256": source_sha256,
            "snapshot_digest": catalog_digest,
            "memory_snapshot_digest": memory_digest,
            "tasks": tasks,
            "root_authority": {
                "grant_id": root_grant_id,
                "token": root_token,
                "expires_at": expires_at,
            },
        }

    def _authorize(
        self,
        fanout_id: str,
        token: str,
        tool: str,
        *,
        task_id: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        projection = self._projection(fanout_id)
        capabilities = self._capabilities(fanout_id)
        grant = next(
            (
                item
                for item in capabilities.get("grants", [])
                if item.get("token_hash") == token_hash(token)
            ),
            None,
        )
        if grant is None:
            self._append_event(
                fanout_id,
                event_type="capability_use_denied",
                actor="unknown",
                decision="deny",
                reason_code="capability_token_unknown",
                task_id=task_id,
            )
            raise FanoutError("capability_token_unknown")
        code: str | None = None
        if grant.get("revoked"):
            code = "capability_revoked"
        elif datetime.fromisoformat(str(grant["expires_at"])) <= datetime.now(timezone.utc):
            code = "capability_expired"
        elif tool not in grant.get("allowed_tools", []):
            code = "capability_tool_denied"
        elif int(grant.get("epoch", 0)) != int(projection["epoch"]):
            code = "stale_workflow_epoch"
        elif int(grant.get("call_count", 0)) >= int(grant.get("max_calls", 0)):
            code = "capability_call_limit_exceeded"
        elif task_id is not None and grant.get("task_id") != task_id:
            code = "capability_task_denied"
        if code:
            self._append_event(
                fanout_id,
                event_type="capability_use_denied",
                actor=str(grant.get("persona") or "root"),
                decision="deny",
                reason_code=code,
                task_id=task_id,
                grant_id=str(grant["grant_id"]),
            )
            raise FanoutError(code)
        grant["call_count"] = int(grant.get("call_count", 0)) + 1
        atomic_write_json(
            self._fanout_root(fanout_id) / "capabilities.runtime.json", capabilities
        )
        return projection, grant

    def context(self, request: dict[str, object]) -> dict[str, object]:
        fanout_id = str(request.get("fanout_id") or "")
        task_id = str(request.get("task_id") or "")
        token = str(request.get("capability_token") or "")
        root = self._fanout_root(fanout_id)
        with locked(root / "fanout.lock"):
            projection, grant = self._authorize(
                fanout_id, token, "reelbrain_get_task_context", task_id=task_id
            )
            requested = request.get("candidate_ids")
            candidate_ids = (
                [str(item) for item in requested]
                if isinstance(requested, list)
                else list(grant["allowed_candidate_ids"])
            )
            if not set(candidate_ids) <= set(grant["allowed_candidate_ids"]):
                self._append_event(
                    fanout_id,
                    event_type="capability_use_denied",
                    actor=str(grant.get("persona")),
                    decision="deny",
                    reason_code="capability_candidate_scope_denied",
                    task_id=task_id,
                    grant_id=str(grant["grant_id"]),
                )
                raise FanoutError("capability_candidate_scope_denied")
            catalog = json.loads((root / "candidate-catalog.json").read_text(encoding="utf-8"))
            memory = json.loads((root / "memory-snapshot.json").read_text(encoding="utf-8"))
            selected = [item for item in catalog if item["candidate_id"] in candidate_ids]
            event = self._append_event(
                fanout_id,
                event_type="task_context_authorized",
                actor=str(grant.get("persona")),
                decision="allow",
                reason_code="capability_context_allowed",
                task_id=task_id,
                grant_id=str(grant["grant_id"]),
                details={"candidate_count": len(selected)},
            )
            return {
                "protocol": PROTOCOL,
                "fanout_id": fanout_id,
                "task_id": task_id,
                "epoch": projection["epoch"],
                "snapshot_digest": projection["catalog_sha256"],
                "memory_snapshot_digest": projection["memory_snapshot_digest"],
                "candidates": selected,
                "preferences": memory["preferences"],
                "current_steering": memory.get("current_steering"),
                "memory_principle": memory["principle"],
                "authorization_receipt_id": event["receipt_id"],
                "remaining_calls": int(grant["max_calls"]) - int(grant["call_count"]),
            }

    @staticmethod
    def _validate_result(
        result: dict[str, object],
        *,
        expected_task_id: str,
        expected_persona: str,
        projection: dict[str, object],
        allowed_ids: set[str],
        allowed_preference_ids: set[str],
    ) -> None:
        required = {
            "task_id",
            "persona",
            "epoch",
            "snapshot_digest",
            "memory_snapshot_digest",
            "selections",
        }
        if not required <= set(result):
            raise FanoutError("persona_result_schema_invalid")
        if result["task_id"] != expected_task_id or result["persona"] != expected_persona:
            raise FanoutError("persona_task_set_mismatch")
        if int(result["epoch"]) != int(projection["epoch"]):
            raise FanoutError("stale_workflow_epoch")
        if result["snapshot_digest"] != projection["catalog_sha256"]:
            raise FanoutError("stale_catalog_snapshot")
        if result["memory_snapshot_digest"] != projection["memory_snapshot_digest"]:
            raise FanoutError("stale_memory_snapshot")
        selections = result["selections"]
        if not isinstance(selections, list) or not selections:
            raise FanoutError("persona_result_schema_invalid")
        for selection in selections:
            if not isinstance(selection, dict) or str(selection.get("candidate_id")) not in allowed_ids:
                raise FanoutError("invented_editorial_id")
            score = selection.get("score")
            if not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
                raise FanoutError("persona_result_schema_invalid")
            used_preferences = selection.get("used_preference_ids", [])
            if not isinstance(used_preferences, list) or not all(
                isinstance(item, str) for item in used_preferences
            ):
                raise FanoutError("persona_result_schema_invalid")
            if not set(used_preferences) <= allowed_preference_ids:
                raise FanoutError("unknown_preference_id")

    def submit(self, request: dict[str, object]) -> dict[str, object]:
        fanout_id = str(request.get("fanout_id") or "")
        token = str(request.get("root_capability_token") or "")
        root = self._fanout_root(fanout_id)
        with locked(root / "fanout.lock"):
            projection, grant = self._authorize(
                fanout_id, token, "reelbrain_submit_fanout"
            )
            expected_revision = request.get("expected_evidence_revision")
            if expected_revision is not None and int(expected_revision) != int(projection["revision"]):
                raise FanoutError("revision_conflict")
            results = request.get("results")
            if not isinstance(results, list) or len(results) != 4:
                raise FanoutError("persona_task_set_mismatch")
            capabilities = self._capabilities(fanout_id)
            persona_grants = {
                str(item["persona"]): item
                for item in capabilities["grants"]
                if item.get("kind") == "persona"
            }
            allowed_ids = set(
                str(item["candidate_id"])
                for item in json.loads((root / "candidate-catalog.json").read_text(encoding="utf-8"))
            )
            memory_snapshot = json.loads(
                (root / "memory-snapshot.json").read_text(encoding="utf-8")
            )
            allowed_preference_ids = {
                str(item["id"]) for item in memory_snapshot.get("preferences", [])
            }
            by_persona = {str(item.get("persona")): item for item in results if isinstance(item, dict)}
            if set(by_persona) != set(PERSONAS):
                raise FanoutError("persona_task_set_mismatch")
            for persona in PERSONAS:
                persona_grant = persona_grants[persona]
                self._validate_result(
                    by_persona[persona],
                    expected_task_id=str(persona_grant["task_id"]),
                    expected_persona=persona,
                    projection=projection,
                    allowed_ids=allowed_ids,
                    allowed_preference_ids=allowed_preference_ids,
                )
            scores: dict[str, list[float]] = {}
            rationales: dict[str, list[str]] = {}
            risks: dict[str, list[str]] = {}
            used_preferences: set[str] = set()
            for result in results:
                for selection in result["selections"]:
                    candidate_id = str(selection["candidate_id"])
                    scores.setdefault(candidate_id, []).append(float(selection["score"]))
                    rationales.setdefault(candidate_id, []).append(str(selection.get("rationale") or ""))
                    risks.setdefault(candidate_id, []).extend(
                        str(item) for item in selection.get("risks", [])
                    )
                    used_preferences.update(
                        str(item) for item in selection.get("used_preference_ids", [])
                    )
            ranked = sorted(
                scores,
                key=lambda candidate_id: (
                    sum(scores[candidate_id]) / len(scores[candidate_id]),
                    len(scores[candidate_id]),
                ),
                reverse=True,
            )
            plan = {
                "protocol": PROTOCOL,
                "fanout_id": fanout_id,
                "epoch": projection["epoch"],
                "source_sha256": projection["source_sha256"],
                "catalog_sha256": projection["catalog_sha256"],
                "memory_snapshot_digest": projection["memory_snapshot_digest"],
                "selected_candidate_ids": ranked[:3],
                "candidate_scores": {
                    candidate_id: sum(values) / len(values)
                    for candidate_id, values in scores.items()
                },
                "rationales": rationales,
                "risks": risks,
                "used_preference_ids": sorted(used_preferences),
                "render_authorized": False,
                "creator_review_required": True,
            }
            submission = {
                "fanout_id": fanout_id,
                "results": results,
                "submitted_at": utc_now(),
            }
            atomic_write_json(root / "submission.json", submission)
            atomic_write_json(root / "editorial-plan.json", plan)
            projection["evidence_state"] = "READY_FOR_RENDER_APPROVAL"
            projection["accepted_submission_sha256"] = digest(submission)
            projection["editorial_plan_sha256"] = digest(plan)
            atomic_write_json(root / "evidence-record.json", projection)
            event = self._append_event(
                fanout_id,
                event_type="fanout_submission_accepted",
                actor="reelbrain-showrunner",
                decision="allow",
                reason_code="grounded_persona_results_accepted",
                grant_id=str(grant["grant_id"]),
                details={"selected_candidate_ids": ranked[:3]},
            )
            current = self._projection(fanout_id)
            return {
                "status": "READY_FOR_RENDER_APPROVAL",
                "fanout_id": fanout_id,
                "epoch": current["epoch"],
                "evidence_revision": current["revision"],
                "submission_digest": projection["accepted_submission_sha256"],
                "plan_path": str(root / "editorial-plan.json"),
                "plan_digest": projection["editorial_plan_sha256"],
                "selected_candidate_ids": ranked[:3],
                "creator_review_required": True,
                "publish_ready": False,
                "receipt_id": event["receipt_id"],
            }

    def steer(self, request: dict[str, object]) -> dict[str, object]:
        fanout_id = str(request.get("fanout_id") or "")
        token = str(request.get("root_capability_token") or "")
        action = str(request.get("action") or "steer")
        if action not in {"steer", "cancel"}:
            raise FanoutError("steering_action_invalid")
        message = str(request.get("message") or request.get("reason") or "").strip()
        if not message:
            raise FanoutError("steering_message_required")
        tool = "reelbrain_steer_fanout" if action == "steer" else "reelbrain_cancel_fanout"
        root = self._fanout_root(fanout_id)
        with locked(root / "fanout.lock"):
            projection, grant = self._authorize(fanout_id, token, tool)
            prior = int(projection["epoch"])
            capabilities = self._capabilities(fanout_id)
            revoked: list[str] = []
            for item in capabilities["grants"]:
                if not item.get("revoked"):
                    item["revoked"] = True
                    revoked.append(str(item["grant_id"]))
            atomic_write_json(root / "capabilities.runtime.json", capabilities)
            projection["epoch"] = prior + 1
            projection["evidence_state"] = "CANCELLED" if action == "cancel" else "REQUIRES_REPLAN"
            atomic_write_json(root / "evidence-record.json", projection)
            event = self._append_event(
                fanout_id,
                event_type="fanout_cancelled" if action == "cancel" else "fanout_steered",
                actor="creator",
                decision="allow",
                reason_code="creator_cancelled" if action == "cancel" else "creator_steering",
                grant_id=str(grant["grant_id"]),
                details={"message": message, "revoked_grant_ids": revoked},
            )
            current = self._projection(fanout_id)
            return {
                "status": current["evidence_state"],
                "fanout_id": fanout_id,
                "previous_epoch": prior,
                "current_epoch": current["epoch"],
                "evidence_revision": current["revision"],
                "revoked_grant_ids": revoked,
                "receipt_id": event["receipt_id"],
            }

    def evidence(self, limit: int = 100) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        fanouts: list[dict[str, object]] = []
        for root in sorted(self.root.glob("fanout_*"), reverse=True):
            projection_path = root / "evidence-record.json"
            if not projection_path.is_file():
                continue
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            fanouts.append(projection)
            events_path = root / "evidence-events.jsonl"
            if events_path.is_file():
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"fanouts": fanouts, "events": rows[:limit]}

    def verify_evidence(self, fanout_id: str) -> dict[str, object]:
        root = self._fanout_root(fanout_id)
        projection = self._projection(fanout_id)
        previous = "sha256:genesis"
        count = 0
        for line in (root / "evidence-events.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            claimed = event.pop("event_hash")
            if event.get("previous_event_hash") != previous or digest(event) != claimed:
                raise FanoutError("evidence_hash_chain_invalid")
            previous = claimed
            count += 1
        if previous != projection["last_event_hash"] or count != projection["last_event_sequence"]:
            raise FanoutError("evidence_projection_mismatch")
        return {"fanout_id": fanout_id, "valid": True, "event_count": count}
