"""Evidence-gated offline configuration optimization (Sleep)."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4


ALLOWED_CONFIGURATION_FAMILIES = frozenset(
    {
        "prompts",
        "persona_skills",
        "rubrics",
        "routing",
        "approved_tool_workflows",
        "editing_heuristics",
        "non_permission_tool_descriptions",
    }
)
PROTECTED_CONFIGURATION_FAMILIES = frozenset(
    {
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
    }
)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted((_thaw(item) for item in value), key=repr)
    return value


def canonical_json(value: object) -> bytes:
    return json.dumps(_thaw(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class ConfigurationBundle:
    bundle_id: str
    version: str
    parent_bundle_id: str | None
    configuration: Mapping[str, object]
    compatibility: Mapping[str, str]
    signature: str | None = None
    signer: str | None = None
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        configuration = _freeze(dict(self.configuration))
        compatibility = _freeze(dict(self.compatibility))
        object.__setattr__(self, "configuration", configuration)
        object.__setattr__(self, "compatibility", compatibility)
        object.__setattr__(
            self,
            "digest",
            sha256(
                canonical_json(
                    {
                        "bundle_id": self.bundle_id,
                        "version": self.version,
                        "parent_bundle_id": self.parent_bundle_id,
                        "configuration": _thaw(configuration),
                        "compatibility": _thaw(compatibility),
                    }
                )
            ).hexdigest(),
        )


class BundleSigner:
    """Reference signer; production adapters can use a hardware-backed key."""

    def __init__(self, *, key_id: str, secret: bytes) -> None:
        if not secret:
            raise ValueError("signing_secret_required")
        self.key_id = key_id
        self._secret = secret

    def sign(self, bundle: ConfigurationBundle) -> ConfigurationBundle:
        signature = hmac.new(self._secret, bundle.digest.encode(), sha256).hexdigest()
        return ConfigurationBundle(
            bundle_id=bundle.bundle_id,
            version=bundle.version,
            parent_bundle_id=bundle.parent_bundle_id,
            configuration=bundle.configuration,
            compatibility=bundle.compatibility,
            signature=signature,
            signer=self.key_id,
        )

    def verify(self, bundle: ConfigurationBundle) -> bool:
        if bundle.signer != self.key_id or bundle.signature is None:
            return False
        current_digest = sha256(
            canonical_json(
                {
                    "bundle_id": bundle.bundle_id,
                    "version": bundle.version,
                    "parent_bundle_id": bundle.parent_bundle_id,
                    "configuration": bundle.configuration,
                    "compatibility": bundle.compatibility,
                }
            )
        ).hexdigest()
        if not hmac.compare_digest(current_digest, bundle.digest):
            return False
        expected = hmac.new(self._secret, current_digest.encode(), sha256).hexdigest()
        return hmac.compare_digest(expected, bundle.signature)


@dataclass(frozen=True)
class PromotionEvidence:
    hidden_task_score_delta: float
    regression_passed: bool
    repeated_trials: int
    repeated_trial_pass_rate: float
    shadow_passed: bool
    canary_passed: bool
    opted_in_canary: bool
    latency_regression_fraction: float
    cost_regression_fraction: float
    rollback_verified: bool
    sealed_fixture_report_id: str
    budget_complete: bool = True
    in_flight_run_count: int = 0


@dataclass(frozen=True)
class PromotionReceipt:
    promotion_id: str
    previous_bundle_id: str | None
    promoted_bundle_id: str
    target: str
    applies_to_new_runs_only: bool
    evidence_digest: str


@dataclass(frozen=True)
class RollbackReceipt:
    rollback_id: str
    failed_bundle_id: str
    restored_bundle_id: str
    reason: str


class SleepPromoter:
    def __init__(
        self,
        *,
        signer: BundleSigner,
        latency_tolerance: float = 0.10,
        cost_tolerance: float = 0.10,
        minimum_trials: int = 3,
        minimum_trial_pass_rate: float = 0.95,
    ) -> None:
        self.signer = signer
        self.latency_tolerance = latency_tolerance
        self.cost_tolerance = cost_tolerance
        self.minimum_trials = minimum_trials
        self.minimum_trial_pass_rate = minimum_trial_pass_rate
        self.active_bundle: ConfigurationBundle | None = None
        self.last_known_good: ConfigurationBundle | None = None
        self.promotions: list[PromotionReceipt] = []
        self.rollbacks: list[RollbackReceipt] = []

    def propose(
        self,
        *,
        parent: ConfigurationBundle | None,
        version: str,
        bounded_changes: Mapping[str, object],
        compatibility: Mapping[str, str],
    ) -> ConfigurationBundle:
        families = set(bounded_changes)
        protected = families & PROTECTED_CONFIGURATION_FAMILIES
        unknown = families - ALLOWED_CONFIGURATION_FAMILIES - PROTECTED_CONFIGURATION_FAMILIES
        if protected:
            raise ValueError(f"protected_sleep_change:{','.join(sorted(protected))}")
        if unknown:
            raise ValueError(f"unknown_sleep_change_family:{','.join(sorted(unknown))}")
        if not bounded_changes:
            raise ValueError("bounded_sleep_change_required")
        configuration = dict(parent.configuration) if parent else {}
        configuration.update(bounded_changes)
        return ConfigurationBundle(
            bundle_id=f"bundle_{uuid4().hex}",
            version=version,
            parent_bundle_id=parent.bundle_id if parent else None,
            configuration=configuration,
            compatibility=compatibility,
        )

    def promote(
        self,
        bundle: ConfigurationBundle,
        evidence: PromotionEvidence,
        *,
        target: str = "managed_canary",
    ) -> PromotionReceipt:
        self._validate_promotion(bundle, evidence, target=target)
        previous = self.active_bundle
        if previous is not None:
            self.last_known_good = previous
        self.active_bundle = bundle
        receipt = PromotionReceipt(
            promotion_id=f"promotion_{uuid4().hex}",
            previous_bundle_id=previous.bundle_id if previous else None,
            promoted_bundle_id=bundle.bundle_id,
            target=target,
            applies_to_new_runs_only=True,
            evidence_digest=sha256(canonical_json(evidence.__dict__)).hexdigest(),
        )
        self.promotions.append(receipt)
        return receipt

    def _validate_promotion(
        self, bundle: ConfigurationBundle, evidence: PromotionEvidence, *, target: str
    ) -> None:
        if not self.signer.verify(bundle):
            raise ValueError("sleep_bundle_signature_invalid")
        self._validate_bundle_boundary(bundle)
        if target != "managed_canary":
            raise ValueError("automatic_local_or_public_promotion_denied")
        if not evidence.opted_in_canary:
            raise ValueError("canary_opt_in_required")
        if evidence.in_flight_run_count:
            raise ValueError("in_flight_run_mutation_denied")
        if evidence.hidden_task_score_delta <= 0:
            raise ValueError("hidden_task_improvement_required")
        if not evidence.regression_passed:
            raise ValueError("regression_gate_failed")
        if evidence.repeated_trials < self.minimum_trials:
            raise ValueError("insufficient_repeated_trials")
        if evidence.repeated_trial_pass_rate < self.minimum_trial_pass_rate:
            raise ValueError("repeated_trial_gate_failed")
        if not evidence.shadow_passed or not evidence.canary_passed:
            raise ValueError("shadow_or_canary_gate_failed")
        if evidence.latency_regression_fraction > self.latency_tolerance:
            raise ValueError("latency_non_regression_gate_failed")
        if evidence.cost_regression_fraction > self.cost_tolerance:
            raise ValueError("cost_non_regression_gate_failed")
        if not evidence.rollback_verified:
            raise ValueError("rollback_verification_required")
        if not evidence.sealed_fixture_report_id.strip():
            raise ValueError("sealed_fixture_report_required")
        if not evidence.budget_complete:
            raise ValueError("budget_truncated_evaluation_denied")

    @staticmethod
    def _validate_bundle_boundary(bundle: ConfigurationBundle) -> None:
        families = set(bundle.configuration)
        protected = families & PROTECTED_CONFIGURATION_FAMILIES
        unknown = families - ALLOWED_CONFIGURATION_FAMILIES - PROTECTED_CONFIGURATION_FAMILIES
        if protected:
            raise ValueError(f"protected_sleep_change:{','.join(sorted(protected))}")
        if unknown:
            raise ValueError(f"unknown_sleep_change_family:{','.join(sorted(unknown))}")
        if not families:
            raise ValueError("bounded_sleep_change_required")
        required_compatibility = {
            "runtime",
            "policy",
            "acp_schema",
            "toolbox_digest",
            "provider",
        }
        missing = sorted(
            key
            for key in required_compatibility
            if not str(bundle.compatibility.get(key, "")).strip()
        )
        if missing:
            raise ValueError(f"sleep_bundle_compatibility_missing:{','.join(missing)}")

    def rollback(self, *, reason: str) -> RollbackReceipt:
        if self.active_bundle is None or self.last_known_good is None:
            raise ValueError("last_known_good_bundle_required")
        failed = self.active_bundle
        restored = self.last_known_good
        self.active_bundle = restored
        receipt = RollbackReceipt(
            rollback_id=f"rollback_{uuid4().hex}",
            failed_bundle_id=failed.bundle_id,
            restored_bundle_id=restored.bundle_id,
            reason=reason,
        )
        self.rollbacks.append(receipt)
        return receipt

    def write_artifacts(
        self,
        output_dir: Path | str,
        *,
        bundle: ConfigurationBundle,
        evidence: PromotionEvidence,
    ) -> dict[str, Path]:
        """Persist the complete evidence bundle required for audit/replay."""

        if not self.signer.verify(bundle):
            raise ValueError("sleep_bundle_signature_invalid")
        self._validate_bundle_boundary(bundle)
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        paths = {
            "signed_configuration_bundle": root / "signed_configuration_bundle.json",
            "bundle_diff": root / "bundle_diff.json",
            "promotion_evidence": root / "promotion_evidence.json",
            "canary_report": root / "canary_report.json",
            "rollback_receipt": root / "rollback_receipt.json",
            "sealed_fixture_report": root / "sealed_fixture_report.json",
        }
        paths["signed_configuration_bundle"].write_text(
            json.dumps(
                {
                    "bundle_id": bundle.bundle_id,
                    "version": bundle.version,
                    "parent_bundle_id": bundle.parent_bundle_id,
                    "configuration": _thaw(bundle.configuration),
                    "compatibility": _thaw(bundle.compatibility),
                    "digest": bundle.digest,
                    "signature": bundle.signature,
                    "signer": bundle.signer,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        paths["bundle_diff"].write_text(
            json.dumps(
                {
                    "parent_bundle_id": bundle.parent_bundle_id,
                    "changed_families": sorted(bundle.configuration),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        paths["promotion_evidence"].write_text(
            json.dumps(evidence.__dict__, indent=2, sort_keys=True), encoding="utf-8"
        )
        paths["canary_report"].write_text(
            json.dumps(
                {
                    "opted_in": evidence.opted_in_canary,
                    "shadow_passed": evidence.shadow_passed,
                    "canary_passed": evidence.canary_passed,
                    "applies_to_new_runs_only": True,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        rollback_payload = (
            self.rollbacks[-1].__dict__
            if self.rollbacks
            else {"rollback_verified": evidence.rollback_verified, "status": "fixture_verified"}
        )
        paths["rollback_receipt"].write_text(
            json.dumps(rollback_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        paths["sealed_fixture_report"].write_text(
            json.dumps(
                {
                    "report_id": evidence.sealed_fixture_report_id,
                    "sealed": bool(evidence.sealed_fixture_report_id),
                    "regression_passed": evidence.regression_passed,
                    "repeated_trials": evidence.repeated_trials,
                    "repeated_trial_pass_rate": evidence.repeated_trial_pass_rate,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return paths


def passing_promotion_evidence() -> PromotionEvidence:
    return PromotionEvidence(
        hidden_task_score_delta=0.05,
        regression_passed=True,
        repeated_trials=5,
        repeated_trial_pass_rate=1.0,
        shadow_passed=True,
        canary_passed=True,
        opted_in_canary=True,
        latency_regression_fraction=0.02,
        cost_regression_fraction=0.01,
        rollback_verified=True,
        sealed_fixture_report_id="sealed-fixture-report-1",
    )
