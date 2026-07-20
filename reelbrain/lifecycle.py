"""Run ledger, non-compensatory verification, and deterministic recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunState(StrEnum):
    DRAFT = "DRAFT"
    AUTO_VERIFIED = "AUTO_VERIFIED"
    CREATOR_REVIEW = "CREATOR_REVIEW"
    PUBLISH_READY = "PUBLISH_READY"
    BLOCKED = "BLOCKED"
    REVOKED = "REVOKED"


GateName = Literal[
    "semantic",
    "caption",
    "media",
    "artifact",
    "rights",
    "privacy",
    "permission",
    "budget",
    "steering",
    "deletion",
    "safety",
    "interruption",
    "retry",
    "deterministic_resume",
]

REQUIRED_GATES: tuple[GateName, ...] = (
    "semantic",
    "caption",
    "media",
    "artifact",
    "rights",
    "privacy",
    "permission",
    "budget",
    "steering",
    "deletion",
    "safety",
    "interruption",
    "retry",
    "deterministic_resume",
)


@dataclass(frozen=True)
class GateResult:
    name: GateName
    passed: bool
    evidence: tuple[str, ...] = ()
    critical: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class StateTransition:
    transition_id: str
    epoch: int
    previous: RunState
    current: RunState
    actor: str
    reason: str
    at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    epoch: int
    stage: str
    payload_digest: str
    completed_idempotency_keys: tuple[str, ...]
    at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class AttemptReport:
    attempt: int
    epoch: int
    gate_results: tuple[GateResult, ...]
    passed: bool
    repaired: bool
    at: str = field(default_factory=utc_now)


@dataclass
class RunLedger:
    run_id: str
    project_id: str
    creator_id: str
    state: RunState = RunState.DRAFT
    epoch: int = 1
    transitions: list[StateTransition] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    attempts: list[AttemptReport] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    costs: list[str] = field(default_factory=list)
    provider_calls: list[str] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    project_manifest: dict[str, object] = field(default_factory=dict)
    _completed_keys: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def create(cls, *, project_id: str, creator_id: str) -> RunLedger:
        return cls(run_id=f"run_{uuid4().hex}", project_id=project_id, creator_id=creator_id)

    def transition(self, target: RunState, *, actor: str, reason: str) -> StateTransition:
        allowed = {
            RunState.DRAFT: {RunState.AUTO_VERIFIED, RunState.BLOCKED, RunState.REVOKED},
            RunState.AUTO_VERIFIED: {RunState.CREATOR_REVIEW, RunState.BLOCKED, RunState.REVOKED},
            RunState.CREATOR_REVIEW: {RunState.PUBLISH_READY, RunState.BLOCKED, RunState.REVOKED},
            RunState.PUBLISH_READY: {RunState.REVOKED},
            RunState.BLOCKED: {RunState.DRAFT, RunState.REVOKED},
            RunState.REVOKED: set(),
        }
        if target not in allowed[self.state]:
            raise ValueError(f"invalid_run_transition:{self.state}->{target}")
        transition = StateTransition(
            transition_id=f"transition_{uuid4().hex}",
            epoch=self.epoch,
            previous=self.state,
            current=target,
            actor=actor,
            reason=reason,
        )
        self.state = target
        self.transitions.append(transition)
        return transition

    def assert_epoch(self, epoch: int) -> None:
        if epoch != self.epoch:
            raise ValueError("stale_workflow_epoch")

    def execute_once(self, *, epoch: int, idempotency_key: str, effect) -> object:
        self.assert_epoch(epoch)
        if idempotency_key in self._completed_keys:
            raise ValueError("duplicate_side_effect")
        result = effect()
        self._completed_keys.add(idempotency_key)
        return result

    def checkpoint(self, *, epoch: int, stage: str, payload: object) -> Checkpoint:
        self.assert_epoch(epoch)
        payload_digest = sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        checkpoint = Checkpoint(
            checkpoint_id=f"checkpoint_{uuid4().hex}",
            epoch=epoch,
            stage=stage,
            payload_digest=payload_digest,
            completed_idempotency_keys=tuple(sorted(self._completed_keys)),
        )
        self.checkpoints.append(checkpoint)
        return checkpoint

    def interrupt(self, *, reason: str) -> Checkpoint:
        checkpoint = self.checkpoint(
            epoch=self.epoch,
            stage="interrupted",
            payload={"reason": reason, "state": self.state},
        )
        self.epoch += 1
        return checkpoint

    def resume(self, checkpoint: Checkpoint, *, expected_payload_digest: str) -> int:
        if checkpoint not in self.checkpoints:
            raise ValueError("checkpoint_not_owned_by_run")
        if checkpoint.payload_digest != expected_payload_digest:
            raise ValueError("checkpoint_digest_mismatch")
        self._completed_keys = set(checkpoint.completed_idempotency_keys)
        return self.epoch

    def add_artifact(self, name: str, content: bytes) -> str:
        digest = sha256(content).hexdigest()
        self.artifact_hashes[name] = digest
        return digest

    def audit_bundle(self) -> dict[str, object]:
        return {
            "run_ledger": {
                "run_id": self.run_id,
                "project_id": self.project_id,
                "creator_id": self.creator_id,
                "state": self.state,
                "epoch": self.epoch,
                "transitions": [asdict(item) for item in self.transitions],
                "checkpoints": [asdict(item) for item in self.checkpoints],
            },
            "verification_report": [asdict(item) for item in self.attempts],
            "project_manifest": dict(self.project_manifest),
            "retry_report": {
                "first_attempt": asdict(self.attempts[0]) if self.attempts else None,
                "repaired_attempts": [
                    asdict(attempt) for attempt in self.attempts if attempt.repaired
                ],
            },
            "artifact_hashes": dict(self.artifact_hashes),
        }

    def set_project_manifest(
        self,
        *,
        sources: dict[str, str],
        rights_manifest_digest: str,
        preference_snapshot_id: str,
        tool_config_digests: dict[str, str],
    ) -> None:
        self.project_manifest = {
            "project_id": self.project_id,
            "creator_id": self.creator_id,
            "sources": dict(sources),
            "rights_manifest_digest": rights_manifest_digest,
            "preference_snapshot_id": preference_snapshot_id,
            "tool_config_digests": dict(tool_config_digests),
            "run_id": self.run_id,
            "epoch": self.epoch,
        }

    def write_artifacts(self, output_dir: Path | str) -> dict[str, Path]:
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        bundle = self.audit_bundle()
        paths = {
            "run_ledger": root / "run_ledger.json",
            "project_manifest": root / "project_manifest.json",
            "verification_report": root / "verification_report.json",
            "retry_report": root / "retry_report.json",
            "checkpoint_snapshots": root / "checkpoint_snapshots.json",
            "audit_bundle": root / "audit_bundle.json",
        }
        paths["run_ledger"].write_text(
            json.dumps(bundle["run_ledger"], indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        paths["project_manifest"].write_text(
            json.dumps(bundle["project_manifest"], indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        paths["verification_report"].write_text(
            json.dumps(bundle["verification_report"], indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        paths["retry_report"].write_text(
            json.dumps(bundle["retry_report"], indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        paths["checkpoint_snapshots"].write_text(
            json.dumps(
                bundle["run_ledger"]["checkpoints"], indent=2, sort_keys=True, default=str
            ),
            encoding="utf-8",
        )
        paths["audit_bundle"].write_text(
            json.dumps(bundle, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return paths


class VerificationHarness:
    """Applies non-compensatory gates and keeps attempts distinct."""

    def __init__(self, ledger: RunLedger) -> None:
        self.ledger = ledger

    def auto_verify(
        self,
        results: Iterable[GateResult],
        *,
        epoch: int,
        repaired: bool = False,
    ) -> AttemptReport:
        self.ledger.assert_epoch(epoch)
        by_name = {result.name: result for result in results}
        missing = [name for name in REQUIRED_GATES if name not in by_name]
        if missing:
            raise ValueError(f"missing_required_gates:{','.join(missing)}")
        ordered = tuple(by_name[name] for name in REQUIRED_GATES)
        passed = all(result.passed for result in ordered)
        report = AttemptReport(
            attempt=len(self.ledger.attempts) + 1,
            epoch=epoch,
            gate_results=ordered,
            passed=passed,
            repaired=repaired,
        )
        self.ledger.attempts.append(report)
        if passed:
            if self.ledger.state == RunState.BLOCKED:
                self.ledger.transition(RunState.DRAFT, actor="harness", reason="repair_started")
            if self.ledger.state == RunState.DRAFT:
                self.ledger.transition(
                    RunState.AUTO_VERIFIED, actor="harness", reason="all_objective_gates_passed"
                )
        else:
            if self.ledger.state != RunState.BLOCKED:
                self.ledger.transition(
                    RunState.BLOCKED, actor="harness", reason="non_compensatory_gate_failed"
                )
        return report

    def request_creator_review(self) -> None:
        self.ledger.transition(
            RunState.CREATOR_REVIEW,
            actor="harness",
            reason="objective_gates_passed_creator_judgment_required",
        )

    def creator_approve(self, approval_receipt: str) -> None:
        if not approval_receipt.strip():
            raise ValueError("creator_approval_receipt_required")
        self.ledger.approvals.append(approval_receipt)
        self.ledger.transition(
            RunState.PUBLISH_READY,
            actor="creator",
            reason="creator_approved_meaning_voice_brand_and_export",
        )

    def revoke(self, reason: str) -> None:
        if self.ledger.state == RunState.REVOKED:
            return
        self.ledger.transition(RunState.REVOKED, actor="harness", reason=reason)


def passing_gate_results() -> tuple[GateResult, ...]:
    return tuple(
        GateResult(name=name, passed=True, evidence=(f"fixture:{name}:pass",))
        for name in REQUIRED_GATES
    )
