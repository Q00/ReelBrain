import json
from pathlib import Path
import subprocess

import pytest

from reelbrain.evidence import ReleaseEvidenceStore
from reelbrain.release import CohortFeedback, FounderDogfoodRun, SemanticFixtureResult


def test_evidence_store_is_append_only_and_writes_honest_failing_reports(tmp_path):
    store = ReleaseEvidenceStore(tmp_path)
    store.record_governance_run(passed=True, receipt="governance-run-1")
    store.record_fixture(
        SemanticFixtureResult("fixture-1", passed=True, must_pass=True)
    )
    store.record_founder_run(
        FounderDogfoodRun("founder-short-1", "short", "PUBLISH_READY", True)
    )
    store.record_cohort_feedback(
        CohortFeedback("creator-1", True, True, 1, True)
    )

    evidence = store.load()
    reports = store.evaluate_and_write()
    verdict = json.loads(reports["release_verification_report"].read_text())

    assert evidence.governance_clean_runs == 1
    assert len(evidence.founder_runs) == 1
    assert len(evidence.cohort) == 1
    assert verdict["passed"] is False
    assert "three_clean_governance_runs" in verdict["failed_checks"]
    assert "founder_three_long_publish_ready" in verdict["failed_checks"]
    assert "private_cohort_size" in verdict["failed_checks"]


def test_old_events_remain_when_more_evidence_is_appended(tmp_path):
    store = ReleaseEvidenceStore(tmp_path)
    store.record_governance_run(passed=True, receipt="run-1")
    first_lines = store.events_path.read_text().splitlines()
    store.record_governance_run(passed=True, receipt="run-2")
    second_lines = store.events_path.read_text().splitlines()

    assert second_lines[:1] == first_lines
    assert len(second_lines) == 2


def test_required_fixture_verifier_records_executed_commands_with_provenance(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        return subprocess.CompletedProcess(command, 0, "1 passed\n", "")

    store = ReleaseEvidenceStore(tmp_path / "evidence")
    results = store.verify_required_fixtures(working_dir=tmp_path, runner=runner)

    assert len(results) == 6
    assert all(result.passed and result.must_pass and result.first_pass for result in results)
    assert all(result.commit == "abc123" for result in results)
    assert all(Path(result.evidence_ref).is_file() for result in results)
    assert len(store.load().fixtures) == 6
    assert calls[0] == ["git", "rev-parse", "HEAD"]


def test_founder_evidence_is_derived_from_real_publish_ready_package(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in (
        "final_short.mp4",
        "captions.srt",
        "captions.vtt",
        "timeline.otio",
        "asset_manifest.json",
        "rights_manifest.json",
        "source_traceability.json",
    ):
        (package / name).write_bytes(b"artifact")
    (package / "validation_report.json").write_text(
        json.dumps(
            {
                "status": "PUBLISH_READY",
                "output_mode": "short",
                "creator_approval_receipt": "founder-approved-1",
            }
        ),
        encoding="utf-8",
    )
    store = ReleaseEvidenceStore(tmp_path / "evidence")

    run = store.record_founder_package(
        package_root=package,
        run_id="founder-short-1",
        output_mode="short",
    )

    assert run.artifact_digest.startswith("sha256:")
    assert run.approval_receipt_id == "founder-approved-1"
    assert store.load().founder_runs == (run,)


def test_founder_evidence_rejects_creator_review_draft(tmp_path):
    package = tmp_path / "draft"
    package.mkdir()
    (package / "validation_report.json").write_text(
        json.dumps({"status": "CREATOR_REVIEW", "output_mode": "short"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="founder_package_not_publish_ready"):
        ReleaseEvidenceStore(tmp_path / "evidence").record_founder_package(
            package_root=package,
            run_id="draft-1",
            output_mode="short",
        )


def test_cohort_response_requires_durable_attestation_and_artifact_digest(tmp_path):
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "creator_id": "creator-1",
                "approves_fidelity_and_personalization": True,
                "willing_to_publish": True,
                "minor_revisions": 1,
                "objective_gates_passed": True,
                "critical_failure": False,
                "package_artifact_digest": "sha256:reviewed-package",
                "attestation_receipt_id": "attestation-1",
            }
        ),
        encoding="utf-8",
    )
    store = ReleaseEvidenceStore(tmp_path / "evidence")

    feedback = store.record_cohort_response(response)

    assert feedback.creator_id == "creator-1"
    assert feedback.attestation_receipt_id == "attestation-1"
    assert store.load().cohort == (feedback,)
