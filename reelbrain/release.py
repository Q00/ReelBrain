"""Deterministic v1 release-bar evaluation and report generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SemanticFixtureResult:
    fixture_id: str
    passed: bool
    must_pass: bool = False
    first_pass: bool = True
    major_defect: bool = False
    critical_failure: bool = False
    slice_name: str = "default"
    command: str | None = None
    evidence_ref: str | None = None
    commit: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class FounderDogfoodRun:
    run_id: str
    output_mode: Literal["short", "long"]
    state: str
    objective_gates_passed: bool
    critical_failure: bool = False
    artifact_digest: str = ""
    package_path: str = ""
    approval_receipt_id: str = ""


@dataclass(frozen=True)
class CohortFeedback:
    creator_id: str
    approves_fidelity_and_personalization: bool
    willing_to_publish: bool
    minor_revisions: int
    objective_gates_passed: bool
    critical_failure: bool = False
    package_artifact_digest: str = ""
    attestation_receipt_id: str = ""


@dataclass(frozen=True)
class ReleaseEvidence:
    platform: str
    architecture: str
    governance_clean_runs: int
    fixtures: tuple[SemanticFixtureResult, ...]
    founder_runs: tuple[FounderDogfoodRun, ...]
    cohort: tuple[CohortFeedback, ...]


@dataclass(frozen=True)
class ReleaseVerdict:
    passed: bool
    checks: dict[str, bool]
    metrics: dict[str, float | int | str]
    failed_checks: tuple[str, ...]
    worst_slice: str | None


class ReleaseBar:
    def evaluate(self, evidence: ReleaseEvidence) -> ReleaseVerdict:
        must_pass = tuple(item for item in evidence.fixtures if item.must_pass)
        remaining = tuple(item for item in evidence.fixtures if not item.must_pass)
        first_pass = tuple(item for item in evidence.fixtures if item.first_pass)
        remaining_pass_rate = (
            sum(item.passed for item in remaining) / len(remaining) if remaining else 1.0
        )
        first_pass_without_major = (
            sum(item.passed and not item.major_defect for item in first_pass) / len(first_pass)
            if first_pass
            else 0.0
        )
        unique_founder = {
            (run.output_mode, run.artifact_digest): run
            for run in evidence.founder_runs
            if run.artifact_digest.strip()
            and run.package_path.strip()
            and run.approval_receipt_id.strip()
        }
        founder_rows = tuple(unique_founder.values())
        unique_cohort = {
            row.creator_id: row
            for row in evidence.cohort
            if row.creator_id.strip()
            and row.package_artifact_digest.strip()
            and row.attestation_receipt_id.strip()
        }
        cohort_rows = tuple(unique_cohort.values())
        short_ready = sum(
            run.output_mode == "short"
            and run.state == "PUBLISH_READY"
            and run.objective_gates_passed
            for run in founder_rows
        )
        long_ready = sum(
            run.output_mode == "long"
            and run.state == "PUBLISH_READY"
            and run.objective_gates_passed
            for run in founder_rows
        )
        cohort_approvals = sum(
            row.approves_fidelity_and_personalization and row.objective_gates_passed
            for row in cohort_rows
        )
        cohort_publish = sum(
            row.willing_to_publish
            and row.minor_revisions <= 1
            and row.objective_gates_passed
            for row in cohort_rows
        )
        critical_failures = sum(item.critical_failure for item in evidence.fixtures)
        critical_failures += sum(run.critical_failure for run in evidence.founder_runs)
        critical_failures += sum(row.critical_failure for row in evidence.cohort)
        checks = {
            "certified_platform": evidence.platform == "macOS"
            and evidence.architecture == "arm64",
            "three_clean_governance_runs": evidence.governance_clean_runs >= 3,
            "all_must_pass_fixtures": bool(must_pass) and all(item.passed for item in must_pass),
            "remaining_semantic_pass_rate": remaining_pass_rate >= 0.95,
            "first_pass_without_major_defect": first_pass_without_major >= 0.90,
            "founder_three_short_publish_ready": short_ready >= 3,
            "founder_three_long_publish_ready": long_ready >= 3,
            "private_cohort_size": len(cohort_rows) >= 10,
            "private_cohort_eight_approve": cohort_approvals >= 8,
            "private_cohort_seven_publish": cohort_publish >= 7,
            "all_objective_gates": all(
                run.objective_gates_passed for run in founder_rows
            )
            and all(row.objective_gates_passed for row in cohort_rows),
            "zero_critical_failures": critical_failures == 0,
        }
        worst_slice = self._worst_slice(evidence.fixtures)
        failed = tuple(name for name, passed in checks.items() if not passed)
        return ReleaseVerdict(
            passed=not failed,
            checks=checks,
            metrics={
                "remaining_semantic_pass_rate": remaining_pass_rate,
                "first_pass_without_major_defect": first_pass_without_major,
                "founder_short_publish_ready": short_ready,
                "founder_long_publish_ready": long_ready,
                "cohort_approvals": cohort_approvals,
                "cohort_publish_ready": cohort_publish,
                "critical_failures": critical_failures,
            },
            failed_checks=failed,
            worst_slice=worst_slice,
        )

    @staticmethod
    def _worst_slice(fixtures: tuple[SemanticFixtureResult, ...]) -> str | None:
        slices: dict[str, list[SemanticFixtureResult]] = {}
        for fixture in fixtures:
            slices.setdefault(fixture.slice_name, []).append(fixture)
        if not slices:
            return None
        return min(
            slices,
            key=lambda name: sum(item.passed for item in slices[name]) / len(slices[name]),
        )

    def write_reports(self, evidence: ReleaseEvidence, output_dir: Path | str) -> dict[str, Path]:
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        verdict = self.evaluate(evidence)
        reports = {
            "release_verification_report": root / "release_verification_report.json",
            "fixture_results": root / "fixture_results.json",
            "founder_dogfood_report": root / "founder_dogfood_report.json",
            "private_cohort_report": root / "private_cohort_report.json",
            "worst_slice_report": root / "worst_slice_report.json",
        }
        reports["release_verification_report"].write_text(
            json.dumps(asdict(verdict), indent=2, sort_keys=True), encoding="utf-8"
        )
        reports["fixture_results"].write_text(
            json.dumps([asdict(item) for item in evidence.fixtures], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        reports["founder_dogfood_report"].write_text(
            json.dumps([asdict(item) for item in evidence.founder_runs], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        reports["private_cohort_report"].write_text(
            json.dumps([asdict(item) for item in evidence.cohort], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        reports["worst_slice_report"].write_text(
            json.dumps({"worst_slice": verdict.worst_slice}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return reports
