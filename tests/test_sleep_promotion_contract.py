from dataclasses import replace

import pytest

from reelbrain.memory import PreferenceScope, PreferenceStore
from reelbrain.sleep import (
    BundleSigner,
    ConfigurationBundle,
    SleepPromoter,
    passing_promotion_evidence,
)


COMPATIBILITY = {
    "runtime": "0.1",
    "policy": "1",
    "acp_schema": "1",
    "toolbox_digest": "sha256:toolbox",
    "provider": "openai-adapter-v1",
}


def signed_bundle(promoter: SleepPromoter, version: str = "1.0.0"):
    proposed = promoter.propose(
        parent=promoter.active_bundle,
        version=version,
        bounded_changes={"prompts": {"showrunner": "preserve complete thoughts"}},
        compatibility=COMPATIBILITY,
    )
    return promoter.signer.sign(proposed)


def make_promoter():
    return SleepPromoter(signer=BundleSigner(key_id="release-key", secret=b"test-secret"))


def test_signed_validated_bundle_promotes_only_to_opted_in_managed_canary():
    promoter = make_promoter()
    bundle = signed_bundle(promoter)

    receipt = promoter.promote(bundle, passing_promotion_evidence())

    assert promoter.active_bundle == bundle
    assert receipt.target == "managed_canary"
    assert receipt.applies_to_new_runs_only is True


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("hidden_task_score_delta", 0.0, "hidden_task_improvement_required"),
        ("regression_passed", False, "regression_gate_failed"),
        ("repeated_trials", 2, "insufficient_repeated_trials"),
        ("repeated_trial_pass_rate", 0.8, "repeated_trial_gate_failed"),
        ("shadow_passed", False, "shadow_or_canary_gate_failed"),
        ("canary_passed", False, "shadow_or_canary_gate_failed"),
        ("opted_in_canary", False, "canary_opt_in_required"),
        ("latency_regression_fraction", 0.2, "latency_non_regression_gate_failed"),
        ("cost_regression_fraction", 0.2, "cost_non_regression_gate_failed"),
        ("rollback_verified", False, "rollback_verification_required"),
        ("sealed_fixture_report_id", "", "sealed_fixture_report_required"),
        ("budget_complete", False, "budget_truncated_evaluation_denied"),
        ("in_flight_run_count", 1, "in_flight_run_mutation_denied"),
    ],
)
def test_every_promotion_gate_is_non_compensatory(field, value, reason):
    promoter = make_promoter()
    bundle = signed_bundle(promoter)
    evidence = replace(passing_promotion_evidence(), **{field: value})

    with pytest.raises(ValueError, match=reason):
        promoter.promote(bundle, evidence)


@pytest.mark.parametrize(
    "family",
    [
        "creator_memory",
        "tool_code",
        "tool_schemas",
        "permissions",
        "secrets",
        "data_retention",
        "consent",
        "publishing",
        "budget_caps",
        "safety_gates",
        "promotion_policy",
        "public_skills_package",
    ],
)
def test_sleep_cannot_propose_protected_changes(family):
    promoter = make_promoter()

    with pytest.raises(ValueError, match="protected_sleep_change"):
        promoter.propose(
            parent=None,
            version="1.0.0",
            bounded_changes={family: {"changed": True}},
            compatibility=COMPATIBILITY,
        )


def test_unsigned_or_tampered_bundle_never_promotes():
    promoter = make_promoter()
    unsigned = promoter.propose(
        parent=None,
        version="1.0.0",
        bounded_changes={"rubrics": {"highlight": "self-contained"}},
        compatibility=COMPATIBILITY,
    )

    with pytest.raises(ValueError, match="sleep_bundle_signature_invalid"):
        promoter.promote(unsigned, passing_promotion_evidence())


def test_local_self_hosted_and_public_skill_auto_promotion_are_denied():
    promoter = make_promoter()
    bundle = signed_bundle(promoter)

    for target in ("local", "self_hosted", "skills.sh"):
        with pytest.raises(ValueError, match="automatic_local_or_public_promotion_denied"):
            promoter.promote(bundle, passing_promotion_evidence(), target=target)


def test_rollback_atomically_restores_last_known_good_bundle():
    promoter = make_promoter()
    first = signed_bundle(promoter, "1.0.0")
    promoter.promote(first, passing_promotion_evidence())
    second = signed_bundle(promoter, "1.1.0")
    promoter.promote(second, passing_promotion_evidence())

    receipt = promoter.rollback(reason="critical_canary_failure")

    assert promoter.active_bundle == first
    assert receipt.failed_bundle_id == second.bundle_id
    assert receipt.restored_bundle_id == first.bundle_id


def test_sleep_does_not_mutate_creator_memory():
    memory = PreferenceStore()
    memory.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="pacing",
        value="natural",
        scope=PreferenceScope(output_mode="short"),
        remember=True,
    )
    before = memory.export_json("creator-1")
    promoter = make_promoter()
    promoter.promote(signed_bundle(promoter), passing_promotion_evidence())

    assert memory.export_json("creator-1") == before


def test_configuration_is_immutable_after_signing():
    promoter = make_promoter()
    bundle = signed_bundle(promoter)

    with pytest.raises(TypeError):
        bundle.configuration["prompts"] = {"showrunner": "mutated"}
