import json

import pytest

from reelbrain.provider_plan import (
    ProviderAuthorizationPlan,
    approve_provider_authorization_plan,
    load_provider_authorization_plan,
)


def test_founder_plan_is_bounded_and_requires_creator_approval(tmp_path):
    plan = ProviderAuthorizationPlan.founder_dogfood(
        project_id="founder-four-videos",
        creator_id="founder",
        source_count=4,
        shorts_per_source=3,
    )

    assert plan.status == "AWAITING_CREATOR_APPROVAL"
    assert plan.hard_cap_cents == 1500
    assert len(plan.calls) == 24  # 4 STT + 4 editorial + 16 images
    with pytest.raises(PermissionError, match="not_approved"):
        plan.require_approved()

    path = plan.write(tmp_path / "provider_plan.json")
    serialized = path.read_text()
    assert "OPEN_API_KEY" not in serialized
    assert json.loads(serialized)["hard_cap_cents"] == 1500


def test_approved_plan_builds_exact_scoped_consent_and_budget_receipts(tmp_path):
    plan = ProviderAuthorizationPlan.founder_dogfood(
        project_id="founder-four-videos",
        creator_id="founder",
        source_count=4,
        shorts_per_source=3,
        source_asset_digests=("a", "b", "c", "d"),
        approved=True,
        approval_receipt_id="founder-openai-cap-15-2026-07-21",
    )
    plan.require_approved()
    loaded = load_provider_authorization_plan(plan.write(tmp_path / "approved.json"))
    call = loaded.call("stt:founder-four-videos:source-01")

    consent = call.consent_receipt(
        project_id=loaded.project_id,
        creator_id=loaded.creator_id,
        approval_receipt_id=loaded.approval_receipt_id,
    )
    budget = call.budget_receipt(
        project_id=loaded.project_id,
        creator_id=loaded.creator_id,
        approval_receipt_id=loaded.approval_receipt_id,
    )

    assert consent["destination"] == "api.openai.com"
    assert consent["invocation_id"] == call.call_id
    assert budget["reserved_amount_cents"] == 125
    assert budget["session_id"] == "runtime:founder-four-videos"


def test_human_gate_approves_only_the_exact_disclosed_cap(tmp_path):
    path = ProviderAuthorizationPlan.founder_dogfood(
        project_id="founder-four-videos",
        creator_id="founder",
        source_count=4,
        shorts_per_source=3,
        source_asset_digests=("a", "b", "c", "d"),
    ).write(tmp_path / "plan.json")

    with pytest.raises(ValueError, match="must_match"):
        approve_provider_authorization_plan(
            path,
            approval_receipt_id="creator-approved",
            approved_hard_cap_cents=1600,
        )

    approved = approve_provider_authorization_plan(
        path,
        approval_receipt_id="creator-approved",
        approved_hard_cap_cents=1500,
    )
    assert approved.status == "APPROVED"
    assert load_provider_authorization_plan(path).approval_receipt_id == "creator-approved"


def test_approved_plan_detects_post_approval_scope_mutation(tmp_path):
    path = ProviderAuthorizationPlan.founder_dogfood(
        project_id="founder-four-videos",
        creator_id="founder",
        source_count=1,
        shorts_per_source=3,
        source_asset_digests=("source-digest",),
        approved=True,
        approval_receipt_id="creator-approved",
    ).write(tmp_path / "plan.json")
    document = json.loads(path.read_text())
    document["calls"][0]["reserved_amount_cents"] = 9999
    path.write_text(json.dumps(document))

    with pytest.raises(PermissionError, match="scope_digest_mismatch"):
        load_provider_authorization_plan(path).require_approved()
