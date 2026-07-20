"""Durable, append-only release evidence collection."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
from time import monotonic
from typing import Callable, Sequence

from .release import (
    CohortFeedback,
    FounderDogfoodRun,
    ReleaseBar,
    ReleaseEvidence,
    SemanticFixtureResult,
)


class ReleaseEvidenceStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "release_evidence.jsonl"

    def append(self, event_type: str, payload: dict[str, object]) -> None:
        event = {"type": event_type, "payload": payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")

    def record_governance_run(self, *, passed: bool, receipt: str) -> None:
        self.append("governance_run", {"passed": passed, "receipt": receipt})

    def record_fixture(self, fixture: SemanticFixtureResult) -> None:
        self.append("semantic_fixture", asdict(fixture))

    def verify_required_fixtures(
        self,
        *,
        working_dir: Path | str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> tuple[SemanticFixtureResult, ...]:
        """Execute the Seed's six machine-verifiable AC commands and persist proof."""

        root = Path(working_dir).resolve()
        fixture_specs: Sequence[tuple[str, str, str]] = (
            ("ac1-short-form", "tests/test_short_form_publish_ready.py", "short-form"),
            ("ac2-long-form", "tests/test_long_form_publish_ready.py", "long-form"),
            ("ac3-memory", "tests/test_creator_memory_contract.py", "personalization"),
            ("ac4-governance", "tests/test_governance_runtime.py", "governance"),
            (
                "ac5-lifecycle",
                "tests/test_verification_and_run_lifecycle.py",
                "lifecycle",
            ),
            ("ac6-sleep", "tests/test_sleep_promotion_contract.py", "sleep"),
        )
        commit_result = runner(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else "unknown"
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        report_dir = self.root / "fixture-runs"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{run_id}.json"
        rows: list[dict[str, object]] = []
        pending: list[tuple[str, bool, str, float, str]] = []
        for fixture_id, test_path, slice_name in fixture_specs:
            command = [sys.executable, "-m", "pytest", "-q", test_path]
            started = monotonic()
            completed = runner(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            duration = monotonic() - started
            output = f"{completed.stdout}\n{completed.stderr}".strip()
            output_digest = sha256(output.encode("utf-8")).hexdigest()
            passed = completed.returncode == 0
            rows.append(
                {
                    "fixture_id": fixture_id,
                    "command": command,
                    "returncode": completed.returncode,
                    "passed": passed,
                    "duration_seconds": duration,
                    "output_sha256": output_digest,
                    "output": output,
                    "slice_name": slice_name,
                }
            )
            pending.append((fixture_id, passed, slice_name, duration, " ".join(command)))
        report_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "working_dir": str(root),
                    "commit": commit,
                    "fixtures": rows,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        results = tuple(
            SemanticFixtureResult(
                fixture_id=f"{fixture_id}:{run_id}",
                passed=passed,
                must_pass=True,
                first_pass=True,
                major_defect=not passed,
                slice_name=slice_name,
                command=command,
                evidence_ref=str(report_path),
                commit=commit,
                duration_seconds=duration,
            )
            for fixture_id, passed, slice_name, duration, command in pending
        )
        for result in results:
            self.record_fixture(result)
        return results

    def record_founder_run(self, run: FounderDogfoodRun) -> None:
        self.append("founder_run", asdict(run))

    def record_founder_package(
        self,
        *,
        package_root: Path | str,
        run_id: str,
        output_mode: str,
    ) -> FounderDogfoodRun:
        root = Path(package_root).expanduser().resolve()
        if output_mode not in {"short", "long"}:
            raise ValueError("founder_output_mode_invalid")
        audit_path = root / "validation_report.json"
        if not audit_path.is_file():
            raise ValueError("founder_validation_report_missing")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if audit.get("status") != "PUBLISH_READY":
            raise ValueError("founder_package_not_publish_ready")
        if audit.get("output_mode") != output_mode:
            raise ValueError("founder_output_mode_mismatch")
        approval_receipt = str(audit.get("creator_approval_receipt") or "").strip()
        if not approval_receipt:
            raise ValueError("founder_creator_approval_missing")
        required = {
            "captions.srt",
            "captions.vtt",
            "timeline.otio",
            "asset_manifest.json",
            "rights_manifest.json",
            "source_traceability.json",
            "validation_report.json",
        }
        video = root / ("final_short.mp4" if output_mode == "short" else "final_long.mp4")
        if output_mode == "long":
            required.update(
                {
                    "chapters.json",
                    "thumbnail.jpg",
                    "render_recipe.json",
                    "argument_map.json",
                    "corrected_transcript.txt",
                    "provenance.json",
                    "cost_receipt.json",
                    "approval_history.json",
                }
            )
        missing = sorted(name for name in required if not (root / name).is_file())
        if missing or not video.is_file():
            raise ValueError(f"founder_package_artifacts_missing:{','.join(missing)}")
        digest = sha256()
        with video.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        run = FounderDogfoodRun(
            run_id=run_id,
            output_mode=output_mode,
            state="PUBLISH_READY",
            objective_gates_passed=True,
            artifact_digest=f"sha256:{digest.hexdigest()}",
            package_path=str(root),
            approval_receipt_id=approval_receipt,
        )
        self.record_founder_run(run)
        return run

    def record_cohort_feedback(self, feedback: CohortFeedback) -> None:
        self.append("cohort_feedback", asdict(feedback))

    def record_cohort_response(self, response_path: Path | str) -> CohortFeedback:
        path = Path(response_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("cohort_response_missing")
        document = json.loads(path.read_text(encoding="utf-8"))
        required = (
            "creator_id",
            "package_artifact_digest",
            "attestation_receipt_id",
        )
        if any(not str(document.get(key) or "").strip() for key in required):
            raise ValueError("cohort_attestation_incomplete")
        feedback = CohortFeedback(
            creator_id=str(document["creator_id"]),
            approves_fidelity_and_personalization=bool(
                document.get("approves_fidelity_and_personalization", False)
            ),
            willing_to_publish=bool(document.get("willing_to_publish", False)),
            minor_revisions=int(document.get("minor_revisions", 0)),
            objective_gates_passed=bool(document.get("objective_gates_passed", False)),
            critical_failure=bool(document.get("critical_failure", False)),
            package_artifact_digest=str(document["package_artifact_digest"]),
            attestation_receipt_id=str(document["attestation_receipt_id"]),
        )
        if feedback.minor_revisions < 0:
            raise ValueError("cohort_minor_revisions_invalid")
        self.record_cohort_feedback(feedback)
        return feedback

    def load(self) -> ReleaseEvidence:
        governance_clean_runs = 0
        fixtures: list[SemanticFixtureResult] = []
        founder_runs: list[FounderDogfoodRun] = []
        cohort: list[CohortFeedback] = []
        if self.events_path.is_file():
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                payload = event["payload"]
                match event["type"]:
                    case "governance_run":
                        governance_clean_runs += int(bool(payload["passed"]))
                    case "semantic_fixture":
                        fixtures.append(SemanticFixtureResult(**payload))
                    case "founder_run":
                        founder_runs.append(FounderDogfoodRun(**payload))
                    case "cohort_feedback":
                        cohort.append(CohortFeedback(**payload))
        return ReleaseEvidence(
            platform="macOS" if platform.system() == "Darwin" else platform.system(),
            architecture=platform.machine(),
            governance_clean_runs=governance_clean_runs,
            fixtures=tuple(fixtures),
            founder_runs=tuple(founder_runs),
            cohort=tuple(cohort),
        )

    def evaluate_and_write(self) -> dict[str, Path]:
        return ReleaseBar().write_reports(self.load(), self.root / "reports")
