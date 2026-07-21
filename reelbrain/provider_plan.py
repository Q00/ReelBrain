"""Serializable provider disclosure and bounded budget plans for dogfood runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Literal


PlanStatus = Literal["AWAITING_CREATOR_APPROVAL", "APPROVED", "REVOKED"]


@dataclass(frozen=True)
class ProviderCallPlan:
    call_id: str
    tool_id: str
    capability: str
    provider: str
    destination: str
    data_categories: tuple[str, ...]
    purpose: str
    expected_retention: str
    expected_cost: str
    reserved_amount_cents: int
    metered_units: int

    def consent_receipt(
        self,
        *,
        project_id: str,
        creator_id: str,
        approval_receipt_id: str,
    ) -> dict[str, object]:
        if not approval_receipt_id.strip():
            raise ValueError("provider_approval_receipt_required")
        return {
            "provider": self.provider,
            "tool_id": self.tool_id,
            "project_id": project_id,
            "creator_id": creator_id,
            "destination": self.destination,
            "invocation_id": self.call_id,
            "approval_receipt_id": approval_receipt_id,
            "data_categories": list(self.data_categories),
            "purpose": self.purpose,
            "expected_retention": self.expected_retention,
            "expected_cost": self.expected_cost,
        }

    def budget_receipt(
        self,
        *,
        project_id: str,
        creator_id: str,
        approval_receipt_id: str,
    ) -> dict[str, object]:
        if not approval_receipt_id.strip():
            raise ValueError("cost_approval_receipt_required")
        return {
            "reservation_id": f"budget:{self.call_id}",
            "requester_id": "reelbrain-runtime",
            "session_id": f"runtime:{project_id}",
            "tool_id": self.tool_id,
            "project_id": project_id,
            "creator_id": creator_id,
            "capabilities": [self.capability],
            "reserved_amount_cents": self.reserved_amount_cents,
            "metered_units": self.metered_units,
            "cost_authorization_receipt_id": approval_receipt_id,
            "state": "reserved",
        }


@dataclass(frozen=True)
class ProviderAuthorizationPlan:
    project_id: str
    creator_id: str
    status: PlanStatus
    hard_cap_cents: int
    calls: tuple[ProviderCallPlan, ...]
    source_asset_digests: tuple[str, ...] = ()
    approval_receipt_id: str = ""
    approved_scope_digest: str = ""

    @classmethod
    def founder_dogfood(
        cls,
        *,
        project_id: str,
        creator_id: str,
        source_count: int,
        shorts_per_source: int,
        source_asset_digests: tuple[str, ...] = (),
        source_active_seconds: tuple[float, ...] = (),
        approved: bool = False,
        approval_receipt_id: str = "",
    ) -> "ProviderAuthorizationPlan":
        if source_count < 1 or not 2 <= shorts_per_source <= 10:
            raise ValueError("invalid_dogfood_provider_plan_shape")
        if (
            not project_id.strip()
            or not creator_id.strip()
            or project_id != project_id.strip()
            or creator_id != creator_id.strip()
        ):
            raise ValueError("provider_plan_identity_fields_must_be_canonical")
        if source_asset_digests and len(source_asset_digests) != source_count:
            raise ValueError("provider_plan_source_digest_count_mismatch")
        if source_active_seconds and len(source_active_seconds) != source_count:
            raise ValueError("provider_plan_active_duration_count_mismatch")
        calls: list[ProviderCallPlan] = []
        for source_index in range(1, source_count + 1):
            suffix = f"source-{source_index:02d}"
            active_seconds = (
                source_active_seconds[source_index - 1]
                if source_active_seconds
                else 0.0
            )
            stt_requests = max(2, 2 * math.ceil(active_seconds / 600))
            estimated_stt_cost = active_seconds / 60 * 0.006 * 2
            calls.append(
                ProviderCallPlan(
                    call_id=f"stt:{project_id}:{suffix}",
                    tool_id="openai-whisper-1",
                    capability="stt:transcribe",
                    provider="openai",
                    destination="api.openai.com",
                    data_categories=("speech_audio_chunks",),
                    purpose="Korean transcription and English subtitle translation",
                    expected_retention=(
                        "OpenAI documents no abuse-monitoring or application-state "
                        "retention for audio transcription/translation endpoints"
                    ),
                    expected_cost=(
                        f"estimated USD {estimated_stt_cost:.2f}; up to USD 1.25 "
                        "reserved for this source"
                    ),
                    reserved_amount_cents=125,
                    metered_units=stt_requests,
                )
            )
            calls.append(
                ProviderCallPlan(
                    call_id=f"editorial:{project_id}:{suffix}",
                    tool_id="openai-responses-editorial",
                    capability="editorial:plan",
                    provider="openai",
                    destination="api.openai.com",
                    data_categories=("timestamped_transcript", "creator_preferences"),
                    purpose="persona fan-out highlight and long-form draft planning",
                    expected_retention=(
                        "store=false; up to 30-day abuse-monitoring retention unless "
                        "the creator API project has Zero Data Retention"
                    ),
                    expected_cost="up to USD 0.50 for this source",
                    reserved_amount_cents=50,
                    metered_units=5,
                )
            )
            output_count = shorts_per_source + 1
            for output_index in range(1, output_count + 1):
                calls.append(
                    ProviderCallPlan(
                        call_id=(
                            f"image:{project_id}:{suffix}:output-{output_index:02d}"
                        ),
                        tool_id="openai-gpt-image-2",
                        capability="image:generate",
                        provider="openai",
                        destination="api.openai.com",
                        data_categories=(
                            "thumbnail_prompt",
                            "brand_context",
                            "transcript_excerpt",
                        ),
                        purpose="GPT Image 2 thumbnail background generation",
                        expected_retention=(
                            "no application state; up to 30-day abuse-monitoring "
                            "retention unless the creator API project has ZDR"
                        ),
                        expected_cost="up to USD 0.50 for one image",
                        reserved_amount_cents=50,
                        metered_units=1,
                    )
                )
        hard_cap = sum(call.reserved_amount_cents for call in calls)
        plan = cls(
            project_id=project_id,
            creator_id=creator_id,
            status="APPROVED" if approved else "AWAITING_CREATOR_APPROVAL",
            hard_cap_cents=hard_cap,
            calls=tuple(calls),
            source_asset_digests=tuple(source_asset_digests),
            approval_receipt_id=approval_receipt_id,
        )
        return replace(plan, approved_scope_digest=plan.scope_digest()) if approved else plan

    def scope_digest(self) -> str:
        payload = {
            "project_id": self.project_id,
            "creator_id": self.creator_id,
            "hard_cap_cents": self.hard_cap_cents,
            "calls": [asdict(call) for call in self.calls],
            "source_asset_digests": list(self.source_asset_digests),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def require_approved(self) -> None:
        if self.status != "APPROVED" or not self.approval_receipt_id.strip():
            raise PermissionError("provider_authorization_plan_not_approved")
        if not self.source_asset_digests:
            raise PermissionError("provider_plan_not_bound_to_source_assets")
        if self.approved_scope_digest != self.scope_digest():
            raise PermissionError("approved_provider_plan_scope_digest_mismatch")
        if sum(call.reserved_amount_cents for call in self.calls) > self.hard_cap_cents:
            raise PermissionError("provider_budget_plan_exceeds_hard_cap")

    def call(self, call_id: str) -> ProviderCallPlan:
        try:
            return next(call for call in self.calls if call.call_id == call_id)
        except StopIteration as exc:
            raise KeyError("provider_call_not_in_plan") from exc

    def write(self, path: Path | str) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8"
        )
        return destination


def load_provider_authorization_plan(path: Path | str) -> ProviderAuthorizationPlan:
    document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return ProviderAuthorizationPlan(
        project_id=document["project_id"],
        creator_id=document["creator_id"],
        status=document["status"],
        hard_cap_cents=int(document["hard_cap_cents"]),
        calls=tuple(ProviderCallPlan(**row) for row in document["calls"]),
        source_asset_digests=tuple(document.get("source_asset_digests", ())),
        approval_receipt_id=document.get("approval_receipt_id", ""),
        approved_scope_digest=document.get("approved_scope_digest", ""),
    )


def approve_provider_authorization_plan(
    path: Path | str,
    *,
    approval_receipt_id: str,
    approved_hard_cap_cents: int,
) -> ProviderAuthorizationPlan:
    plan = load_provider_authorization_plan(path)
    if approved_hard_cap_cents != plan.hard_cap_cents:
        raise ValueError("approved_provider_cap_must_match_disclosed_plan")
    approved = replace(
        plan,
        status="APPROVED",
        approval_receipt_id=approval_receipt_id,
        approved_scope_digest=plan.scope_digest(),
    )
    approved.require_approved()
    approved.write(path)
    return approved
