"""Consent-first, scoped creator preference memory.

Memory is a revisable behavioral prior, never source evidence. Episode feedback
does not become durable until the creator explicitly confirms a proposed
preference or asks ReelBrain to remember it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4

PreferenceStatus = Literal["active", "disabled", "deleted"]
FeedbackKind = Literal["episode", "remember", "confirm", "override", "delete"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PreferenceScope:
    """Applicability boundary for a creator preference."""

    output_mode: str | None = None
    content_kind: str | None = None
    language: str | None = None

    def matches(self, context: PreferenceScope) -> bool:
        for field_name in ("output_mode", "content_kind", "language"):
            expected = getattr(self, field_name)
            actual = getattr(context, field_name)
            if expected is not None and expected != actual:
                return False
        return True

    @property
    def specificity(self) -> int:
        return sum(value is not None for value in asdict(self).values())


@dataclass(frozen=True)
class FeedbackEvent:
    event_id: str
    creator_id: str
    project_id: str
    category: str
    value: str
    scope: PreferenceScope
    kind: FeedbackKind = "episode"
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now())


@dataclass(frozen=True)
class Preference:
    preference_id: str
    creator_id: str
    category: str
    value: str
    scope: PreferenceScope
    confidence: float
    provenance_event_ids: tuple[str, ...]
    version: int = 1
    status: PreferenceStatus = "active"
    explicit: bool = True
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now = utc_now()
        if not self.created_at:
            object.__setattr__(self, "created_at", now)
        if not self.updated_at:
            object.__setattr__(self, "updated_at", now)


@dataclass(frozen=True)
class PreferenceProposal:
    creator_id: str
    category: str
    value: str
    scope: PreferenceScope
    evidence_event_ids: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class PreferenceTombstone:
    preference_id: str
    creator_id: str
    deleted_at: str
    deletion_receipt_id: str


@dataclass(frozen=True)
class PreferenceResolution:
    category: str
    value: str | None
    source: str
    preference_id: str | None = None
    reason: str | None = None


class PreferenceStore:
    """In-memory reference store with explicit persistence boundaries.

    Production adapters can persist the exported JSON locally. The API keeps
    deletion tombstones separate from content-bearing preferences so deleted
    values cannot be reconstructed by replay or Sleep.
    """

    def __init__(self) -> None:
        self._events: list[FeedbackEvent] = []
        self._preferences: dict[str, Preference] = {}
        self._tombstones: dict[str, PreferenceTombstone] = {}

    @property
    def events(self) -> tuple[FeedbackEvent, ...]:
        return tuple(self._events)

    @property
    def tombstones(self) -> tuple[PreferenceTombstone, ...]:
        return tuple(self._tombstones.values())

    def record_feedback(
        self,
        *,
        creator_id: str,
        project_id: str,
        category: str,
        value: str,
        scope: PreferenceScope,
        remember: bool = False,
    ) -> FeedbackEvent:
        event = FeedbackEvent(
            event_id=f"feedback_{uuid4().hex}",
            creator_id=creator_id,
            project_id=project_id,
            category=category.strip(),
            value=value.strip(),
            scope=scope,
            kind="remember" if remember else "episode",
        )
        self._events.append(event)
        if remember:
            self._activate_from_events((event,), confidence=1.0)
        return event

    def propose(
        self,
        *,
        creator_id: str,
        category: str,
        scope: PreferenceScope,
        minimum_examples: int = 2,
    ) -> PreferenceProposal | None:
        matching = [
            event
            for event in self._events
            if event.creator_id == creator_id
            and event.category == category
            and event.scope == scope
            and event.kind == "episode"
        ]
        if len(matching) < minimum_examples:
            return None
        counts: dict[str, list[FeedbackEvent]] = {}
        for event in matching:
            counts.setdefault(event.value, []).append(event)
        value, evidence = max(counts.items(), key=lambda item: len(item[1]))
        if len(evidence) < minimum_examples:
            return None
        confidence = len(evidence) / len(matching)
        return PreferenceProposal(
            creator_id=creator_id,
            category=category,
            value=value,
            scope=scope,
            evidence_event_ids=tuple(event.event_id for event in evidence),
            confidence=confidence,
        )

    def confirm(self, proposal: PreferenceProposal) -> Preference:
        evidence = tuple(
            event for event in self._events if event.event_id in proposal.evidence_event_ids
        )
        if len(evidence) != len(proposal.evidence_event_ids):
            raise ValueError("proposal_evidence_missing")
        confirmation = FeedbackEvent(
            event_id=f"feedback_{uuid4().hex}",
            creator_id=proposal.creator_id,
            project_id="preference-confirmation",
            category=proposal.category,
            value=proposal.value,
            scope=proposal.scope,
            kind="confirm",
        )
        self._events.append(confirmation)
        return self._activate_from_events(
            (*evidence, confirmation), confidence=proposal.confidence
        )

    def _activate_from_events(
        self, events: Iterable[FeedbackEvent], *, confidence: float
    ) -> Preference:
        event_list = tuple(events)
        if not event_list:
            raise ValueError("preference_evidence_required")
        first = event_list[0]
        preference = Preference(
            preference_id=f"pref_{uuid4().hex}",
            creator_id=first.creator_id,
            category=first.category,
            value=first.value,
            scope=first.scope,
            confidence=confidence,
            provenance_event_ids=tuple(event.event_id for event in event_list),
        )
        self._preferences[preference.preference_id] = preference
        return preference

    def inspect(self, creator_id: str, *, include_disabled: bool = False) -> tuple[Preference, ...]:
        return tuple(
            preference
            for preference in self._preferences.values()
            if preference.creator_id == creator_id
            and preference.status != "deleted"
            and (include_disabled or preference.status == "active")
        )

    def edit(
        self,
        preference_id: str,
        *,
        value: str | None = None,
        scope: PreferenceScope | None = None,
    ) -> Preference:
        current = self._require_active_or_disabled(preference_id)
        updated = replace(
            current,
            value=current.value if value is None else value.strip(),
            scope=current.scope if scope is None else scope,
            version=current.version + 1,
            updated_at=utc_now(),
        )
        self._preferences[preference_id] = updated
        return updated

    def set_enabled(self, preference_id: str, enabled: bool) -> Preference:
        current = self._require_active_or_disabled(preference_id)
        updated = replace(
            current,
            status="active" if enabled else "disabled",
            version=current.version + 1,
            updated_at=utc_now(),
        )
        self._preferences[preference_id] = updated
        return updated

    def delete(self, preference_id: str) -> PreferenceTombstone:
        current = self._require_active_or_disabled(preference_id)
        tombstone = PreferenceTombstone(
            preference_id=preference_id,
            creator_id=current.creator_id,
            deleted_at=utc_now(),
            deletion_receipt_id=f"delete_{uuid4().hex}",
        )
        self._tombstones[preference_id] = tombstone
        # Remove content-bearing value rather than preserving a soft-deleted row.
        del self._preferences[preference_id]
        return tombstone

    def resolve(
        self,
        *,
        creator_id: str,
        category: str,
        context: PreferenceScope,
        current_steering: str | None = None,
        edit_override: str | None = None,
        default: str | None = None,
    ) -> PreferenceResolution:
        if current_steering is not None:
            return PreferenceResolution(category, current_steering, "current_steering")
        if edit_override is not None:
            return PreferenceResolution(category, edit_override, "edit_override")
        candidates = [
            preference
            for preference in self._preferences.values()
            if preference.creator_id == creator_id
            and preference.category == category
            and preference.status == "active"
            and preference.scope.matches(context)
        ]
        if candidates:
            selected = max(
                candidates,
                key=lambda preference: (
                    preference.scope.specificity,
                    preference.explicit,
                    preference.version,
                    preference.updated_at,
                ),
            )
            return PreferenceResolution(
                category,
                selected.value,
                "explicit_preference" if selected.explicit else "confirmed_inferred_preference",
                selected.preference_id,
            )
        return PreferenceResolution(
            category,
            default,
            "default" if default is not None else "abstain",
            reason=None if default is not None else "no_relevant_confirmed_preference",
        )

    def export_json(self, creator_id: str) -> str:
        preferences = [
            {
                **asdict(preference),
                "scope": asdict(preference.scope),
            }
            for preference in self.inspect(creator_id, include_disabled=True)
        ]
        return json.dumps({"version": 1, "preferences": preferences}, sort_keys=True)

    def import_json(self, creator_id: str, payload: str) -> tuple[Preference, ...]:
        document = json.loads(payload)
        if document.get("version") != 1:
            raise ValueError("unsupported_preference_export_version")
        imported: list[Preference] = []
        for raw in document.get("preferences", []):
            if raw["creator_id"] != creator_id:
                raise ValueError("cross_creator_preference_import_denied")
            if raw["preference_id"] in self._tombstones:
                raise ValueError("deleted_preference_resurrection_denied")
            scope = PreferenceScope(**raw.pop("scope"))
            preference = Preference(scope=scope, **raw)
            self._preferences[preference.preference_id] = preference
            imported.append(preference)
        return tuple(imported)

    def write_artifacts(
        self,
        output_dir: Path | str,
        *,
        creator_id: str,
        evaluation_category: str,
        evaluation_context: PreferenceScope,
        frozen_baseline_value: str,
    ) -> dict[str, Path]:
        """Persist the inspectable memory contract and transfer evaluation."""

        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        preferences = self.inspect(creator_id, include_disabled=True)
        personalized = self.resolve(
            creator_id=creator_id,
            category=evaluation_category,
            context=evaluation_context,
            default=frozen_baseline_value,
        )
        artifacts = {
            "preference_ledger": root / "preference_ledger.json",
            "feedback_events": root / "feedback_events.json",
            "preference_snapshots": root / "preference_snapshots.json",
            "deletion_tombstones": root / "deletion_tombstones.json",
            "personalized_vs_baseline_evaluation": root
            / "personalized_vs_baseline_evaluation.json",
        }
        artifacts["preference_ledger"].write_text(
            json.dumps(
                [
                    {**asdict(item), "scope": asdict(item.scope)}
                    for item in preferences
                ],
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        creator_events = [event for event in self._events if event.creator_id == creator_id]
        artifacts["feedback_events"].write_text(
            json.dumps(
                [{**asdict(item), "scope": asdict(item.scope)} for item in creator_events],
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        artifacts["preference_snapshots"].write_text(
            json.dumps(
                {
                    "creator_id": creator_id,
                    "snapshot_version": max((item.version for item in preferences), default=0),
                    "active_preference_ids": [
                        item.preference_id for item in preferences if item.status == "active"
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        artifacts["deletion_tombstones"].write_text(
            json.dumps(
                [asdict(item) for item in self._tombstones.values() if item.creator_id == creator_id],
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        artifacts["personalized_vs_baseline_evaluation"].write_text(
            json.dumps(
                {
                    "creator_id": creator_id,
                    "category": evaluation_category,
                    "context": asdict(evaluation_context),
                    "frozen_baseline": frozen_baseline_value,
                    "personalized_value": personalized.value,
                    "preference_applied": personalized.source
                    in {"explicit_preference", "confirmed_inferred_preference"},
                    "source": personalized.source,
                    "preference_id": personalized.preference_id,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return artifacts

    def _require_active_or_disabled(self, preference_id: str) -> Preference:
        if preference_id in self._tombstones:
            raise ValueError("preference_deleted")
        try:
            return self._preferences[preference_id]
        except KeyError as exc:
            raise KeyError("preference_not_found") from exc
