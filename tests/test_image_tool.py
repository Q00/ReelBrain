import base64
import json

import pytest

from reelbrain.image_tool import GPTImage2Tool
from reelbrain.runtime_guard import RuntimeGuard


PNG = b"\x89PNG\r\n\x1a\nfixture"


class FixtureTransport:
    def __init__(self):
        self.calls = []

    def generate(self, *, api_key, payload):
        self.calls.append((api_key, payload))
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


def consent():
    return {
        "provider": "openai",
        "tool_id": "openai-gpt-image-2",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "destination": "api.openai.com",
        "invocation_id": "image-call-1",
        "approval_receipt_id": "provider-consent-1",
        "data_categories": ["prompt", "brand_context"],
        "purpose": "thumbnail generation",
        "expected_retention": "provider request lifecycle",
        "expected_cost": "approved image generation call",
    }


def budget():
    return {
        "reservation_id": "budget-image-1",
        "requester_id": "reelbrain-runtime",
        "session_id": "runtime:project-1",
        "tool_id": "openai-gpt-image-2",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "capabilities": ["image:generate"],
        "reserved_amount_cents": 10,
        "metered_units": 1,
        "cost_authorization_receipt_id": "cost-approved-image-1",
        "state": "reserved",
    }


def test_gpt_image_2_requires_consent_and_writes_image_plus_provenance(tmp_path):
    transport = FixtureTransport()
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    tool = GPTImage2Tool(transport)

    artifact = tool.generate_thumbnail(
        prompt="A source-faithful Ouroboros architecture thumbnail",
        output_path=tmp_path / "thumbnail.png",
        guard=guard,
        provider_consent_receipt=consent(),
        budget_reservation_receipt=budget(),
        secret_resolver=lambda ref: "test-api-key",
        creator_approval_receipt="creator-approved-thumbnail-1",
    )

    assert artifact.image_path.read_bytes() == PNG
    provenance = json.loads(artifact.provenance_path.read_text())
    assert provenance["model"] == "gpt-image-2"
    assert provenance["synthetic_media_review_required"] is True
    assert transport.calls[0][1]["model"] == "gpt-image-2"
    assert "test-api-key" not in json.dumps(guard.capability_receipts)
    assert "test-api-key" not in artifact.provenance_path.read_text()
    assert [row["state"] for row in guard.budget_ledger] == ["reserved", "consumed"]
    assert guard.approval_records[0]["approval_receipt_id"] == "provider-consent-1"


def test_gpt_image_2_denies_missing_provider_consent_before_secret_resolution(tmp_path):
    transport = FixtureTransport()
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    secret_calls = []

    with pytest.raises(PermissionError, match="provider_consent_required"):
        GPTImage2Tool(transport).generate_thumbnail(
            prompt="thumbnail",
            output_path=tmp_path / "thumbnail.png",
            guard=guard,
            provider_consent_receipt={},
            budget_reservation_receipt=budget(),
            secret_resolver=lambda ref: secret_calls.append(ref) or "secret",
            creator_approval_receipt="approved",
        )

    assert secret_calls == []
    assert transport.calls == []


def test_gpt_image_2_requires_creator_approval(tmp_path):
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )

    with pytest.raises(ValueError, match="creator_image_approval_required"):
        GPTImage2Tool(FixtureTransport()).generate_thumbnail(
            prompt="thumbnail",
            output_path=tmp_path / "thumbnail.png",
            guard=guard,
            provider_consent_receipt=consent(),
            budget_reservation_receipt=budget(),
            secret_resolver=lambda ref: "secret",
            creator_approval_receipt="",
        )
