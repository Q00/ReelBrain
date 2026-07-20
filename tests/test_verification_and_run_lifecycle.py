import pytest

from reelbrain.lifecycle import (
    GateResult,
    REQUIRED_GATES,
    RunLedger,
    RunState,
    VerificationHarness,
    passing_gate_results,
)


def test_publish_ready_requires_every_objective_gate_and_creator_approval():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    harness = VerificationHarness(ledger)

    report = harness.auto_verify(passing_gate_results(), epoch=ledger.epoch)
    harness.request_creator_review()
    harness.creator_approve("approval_creator_1")

    assert report.passed is True
    assert ledger.state == RunState.PUBLISH_READY
    assert [transition.current for transition in ledger.transitions] == [
        RunState.AUTO_VERIFIED,
        RunState.CREATOR_REVIEW,
        RunState.PUBLISH_READY,
    ]


def test_one_failed_gate_blocks_even_when_every_other_gate_passes():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    harness = VerificationHarness(ledger)
    results = list(passing_gate_results())
    results[REQUIRED_GATES.index("caption")] = GateResult(
        name="caption", passed=False, reason="meaning_changing_error"
    )

    report = harness.auto_verify(results, epoch=ledger.epoch)

    assert report.passed is False
    assert ledger.state == RunState.BLOCKED
    with pytest.raises(ValueError, match="invalid_run_transition"):
        harness.creator_approve("approval")


def test_missing_gate_is_not_treated_as_pass_or_compensated():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    harness = VerificationHarness(ledger)

    with pytest.raises(ValueError, match="missing_required_gates:deletion"):
        harness.auto_verify(
            [result for result in passing_gate_results() if result.name != "deletion"],
            epoch=ledger.epoch,
        )

    assert ledger.state == RunState.DRAFT


def test_first_attempt_and_repaired_attempt_are_reported_separately():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    harness = VerificationHarness(ledger)
    first = list(passing_gate_results())
    first[0] = GateResult(name="semantic", passed=False, reason="missing_caveat")

    harness.auto_verify(first, epoch=ledger.epoch)
    repaired = harness.auto_verify(passing_gate_results(), epoch=ledger.epoch, repaired=True)
    bundle = ledger.audit_bundle()

    assert repaired.attempt == 2
    assert ledger.state == RunState.AUTO_VERIFIED
    assert bundle["retry_report"]["first_attempt"]["passed"] is False
    assert bundle["retry_report"]["repaired_attempts"][0]["passed"] is True


def test_stale_epoch_cannot_commit_side_effects_after_interruption():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    calls = []
    old_epoch = ledger.epoch
    ledger.interrupt(reason="creator_changed_caption_style")

    with pytest.raises(ValueError, match="stale_workflow_epoch"):
        ledger.execute_once(
            epoch=old_epoch,
            idempotency_key="render:short-1",
            effect=lambda: calls.append("rendered"),
        )

    assert calls == []


def test_resume_restores_idempotency_fence_and_rejects_duplicate_effect():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    calls = []
    ledger.execute_once(
        epoch=ledger.epoch,
        idempotency_key="provider:transcribe:source-1",
        effect=lambda: calls.append("provider-called"),
    )
    checkpoint = ledger.interrupt(reason="creator_pause")
    ledger.resume(checkpoint, expected_payload_digest=checkpoint.payload_digest)

    with pytest.raises(ValueError, match="duplicate_side_effect"):
        ledger.execute_once(
            epoch=ledger.epoch,
            idempotency_key="provider:transcribe:source-1",
            effect=lambda: calls.append("provider-called-again"),
        )

    assert calls == ["provider-called"]


def test_checkpoint_digest_mismatch_fails_closed():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    checkpoint = ledger.interrupt(reason="pause")

    with pytest.raises(ValueError, match="checkpoint_digest_mismatch"):
        ledger.resume(checkpoint, expected_payload_digest="tampered")


def test_artifact_hashes_and_audit_bundle_are_reproducible():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    first = ledger.add_artifact("manifest.json", b'{"project":"project-1"}')
    second = ledger.add_artifact("manifest-copy.json", b'{"project":"project-1"}')

    bundle = ledger.audit_bundle()

    assert first == second
    assert bundle["run_ledger"]["run_id"] == ledger.run_id
    assert bundle["artifact_hashes"]["manifest.json"] == first


def test_publish_ready_can_be_revoked_but_never_reopened_silently():
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    harness = VerificationHarness(ledger)
    harness.auto_verify(passing_gate_results(), epoch=ledger.epoch)
    harness.request_creator_review()
    harness.creator_approve("approval_creator_1")

    harness.revoke("rights_consent_revoked")

    assert ledger.state == RunState.REVOKED
    with pytest.raises(ValueError, match="invalid_run_transition"):
        ledger.transition(RunState.DRAFT, actor="agent", reason="silent_reopen")


def test_writes_project_manifest_and_every_declared_lifecycle_artifact(tmp_path):
    ledger = RunLedger.create(project_id="project-1", creator_id="creator-1")
    ledger.set_project_manifest(
        sources={"primary_video": "sha256:source"},
        rights_manifest_digest="sha256:rights",
        preference_snapshot_id="snapshot-1",
        tool_config_digests={"ffmpeg": "sha256:ffmpeg"},
    )
    harness = VerificationHarness(ledger)
    harness.auto_verify(passing_gate_results(), epoch=ledger.epoch)
    ledger.checkpoint(epoch=ledger.epoch, stage="rendered", payload={"artifact": "short"})

    artifacts = ledger.write_artifacts(tmp_path)

    assert set(artifacts) == {
        "run_ledger",
        "project_manifest",
        "verification_report",
        "retry_report",
        "checkpoint_snapshots",
        "audit_bundle",
    }
    assert all(path.is_file() for path in artifacts.values())
