import json

from reelbrain.release import (
    CohortFeedback,
    FounderDogfoodRun,
    ReleaseBar,
    ReleaseEvidence,
    SemanticFixtureResult,
)


def passing_evidence():
    fixtures = tuple(
        SemanticFixtureResult(
            fixture_id=f"fixture-{index}",
            passed=True,
            must_pass=index < 5,
            slice_name="mixed-language" if index % 2 else "technical-code",
        )
        for index in range(40)
    )
    founder = tuple(
        FounderDogfoodRun(
            run_id=f"founder-short-{index}",
            output_mode="short",
            state="PUBLISH_READY",
            objective_gates_passed=True,
        )
        for index in range(3)
    ) + tuple(
        FounderDogfoodRun(
            run_id=f"founder-long-{index}",
            output_mode="long",
            state="PUBLISH_READY",
            objective_gates_passed=True,
        )
        for index in range(3)
    )
    cohort = tuple(
        CohortFeedback(
            creator_id=f"creator-{index}",
            approves_fidelity_and_personalization=index < 8,
            willing_to_publish=index < 7,
            minor_revisions=1 if index < 7 else 2,
            objective_gates_passed=True,
        )
        for index in range(10)
    )
    return ReleaseEvidence(
        platform="macOS",
        architecture="arm64",
        governance_clean_runs=3,
        fixtures=fixtures,
        founder_runs=founder,
        cohort=cohort,
    )


def test_exact_release_thresholds_pass_and_write_all_reports(tmp_path):
    bar = ReleaseBar()
    evidence = passing_evidence()

    verdict = bar.evaluate(evidence)
    reports = bar.write_reports(evidence, tmp_path)

    assert verdict.passed is True
    assert verdict.metrics["founder_short_publish_ready"] == 3
    assert verdict.metrics["founder_long_publish_ready"] == 3
    assert verdict.metrics["cohort_approvals"] == 8
    assert verdict.metrics["cohort_publish_ready"] == 7
    assert set(reports) == {
        "release_verification_report",
        "fixture_results",
        "founder_dogfood_report",
        "private_cohort_report",
        "worst_slice_report",
    }
    assert all(path.is_file() for path in reports.values())
    assert json.loads(reports["release_verification_report"].read_text())["passed"] is True


def test_one_critical_failure_blocks_release_even_when_every_rate_passes():
    evidence = passing_evidence()
    fixtures = list(evidence.fixtures)
    fixtures[0] = SemanticFixtureResult(
        fixture_id=fixtures[0].fixture_id,
        passed=True,
        must_pass=True,
        critical_failure=True,
        slice_name=fixtures[0].slice_name,
    )
    evidence = ReleaseEvidence(**{**evidence.__dict__, "fixtures": tuple(fixtures)})

    verdict = ReleaseBar().evaluate(evidence)

    assert verdict.passed is False
    assert "zero_critical_failures" in verdict.failed_checks


def test_release_fails_below_each_founder_and_cohort_threshold():
    evidence = passing_evidence()
    evidence = ReleaseEvidence(
        **{
            **evidence.__dict__,
            "founder_runs": evidence.founder_runs[:-1],
            "cohort": tuple(
                CohortFeedback(
                    **{
                        **row.__dict__,
                        "approves_fidelity_and_personalization": index < 7,
                        "willing_to_publish": index < 6,
                    }
                )
                for index, row in enumerate(evidence.cohort)
            ),
        }
    )

    verdict = ReleaseBar().evaluate(evidence)

    assert verdict.passed is False
    assert "founder_three_long_publish_ready" in verdict.failed_checks
    assert "private_cohort_eight_approve" in verdict.failed_checks
    assert "private_cohort_seven_publish" in verdict.failed_checks


def test_must_pass_fixture_cannot_be_compensated_by_other_successes():
    evidence = passing_evidence()
    fixtures = list(evidence.fixtures)
    fixtures[0] = SemanticFixtureResult(
        fixture_id=fixtures[0].fixture_id,
        passed=False,
        must_pass=True,
        slice_name="critical-governance",
    )
    evidence = ReleaseEvidence(**{**evidence.__dict__, "fixtures": tuple(fixtures)})

    verdict = ReleaseBar().evaluate(evidence)

    assert verdict.passed is False
    assert "all_must_pass_fixtures" in verdict.failed_checks
    assert verdict.worst_slice == "critical-governance"


def test_non_certified_platform_is_reported_not_silently_accepted():
    evidence = passing_evidence()
    evidence = ReleaseEvidence(**{**evidence.__dict__, "platform": "Linux", "architecture": "x86_64"})

    verdict = ReleaseBar().evaluate(evidence)

    assert verdict.passed is False
    assert "certified_platform" in verdict.failed_checks
