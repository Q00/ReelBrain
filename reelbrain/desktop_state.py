"""Persistent creator-facing state for ReelBrain Desktop.

This module is deliberately local-only. It gives the desktop shell a small,
closed service surface while keeping durable taste decisions in ReelBrain's
consent-first memory implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Iterator

import fcntl

from .memory import (
    DeletionFenceRegistry,
    PreferenceProposal,
    PreferenceScope,
    PreferenceStore,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def proposal_id(proposal: PreferenceProposal) -> str:
    payload = "|".join(
        (
            proposal.creator_id,
            proposal.category,
            proposal.value,
            json.dumps(asdict(proposal.scope), sort_keys=True),
            *proposal.evidence_event_ids,
        )
    )
    return f"proposal_{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


class DesktopMemoryService:
    """Revisioned, restart-safe adapter around :class:`PreferenceStore`."""

    def __init__(self, workspace: Path | str, creator_id: str = "creator-founder") -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.creator_id = creator_id
        self.root = self.workspace / ".reelbrain" / "desktop" / "memory"
        self.path = self.root / f"{creator_id}.json"
        self.lock_path = self.root / f"{creator_id}.lock"

    def _new_document(self) -> dict[str, object]:
        store = PreferenceStore(deletion_fences=DeletionFenceRegistry())
        bootstrap = (
            (
                "Hooks",
                "Technical tension over sensational hooks",
                PreferenceScope(output_mode="short", content_kind="technical"),
            ),
            (
                "Context",
                "Preserve complete technical caveats",
                PreferenceScope(content_kind="technical"),
            ),
            (
                "Captions",
                "Korean + English captions",
                PreferenceScope(language="bilingual"),
            ),
        )
        for category, value, scope in bootstrap:
            store.record_feedback(
                creator_id=self.creator_id,
                project_id="founder-dogfood-bootstrap",
                category=category,
                value=value,
                scope=scope,
                remember=True,
            )
        return {
            "version": 1,
            "revision": 1,
            "creator_id": self.creator_id,
            "bootstrap": "founder-explicit-preferences-v1",
            "store": store.to_document(self.creator_id),
            "audit": [
                {
                    "event_type": "memory_bootstrapped",
                    "actor": "creator-founder",
                    "reason": "Previously explicit founder dogfood preferences",
                    "at": utc_now(),
                }
            ],
            "updated_at": utc_now(),
        }

    def _load_document(self) -> dict[str, object]:
        if not self.path.is_file():
            document = self._new_document()
            atomic_write_json(self.path, document)
            return document
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if document.get("version") != 1 or document.get("creator_id") != self.creator_id:
            raise ValueError("desktop_memory_document_invalid")
        return document

    def _load_store(self, document: dict[str, object]) -> PreferenceStore:
        raw_store = document.get("store")
        if not isinstance(raw_store, dict):
            raise ValueError("desktop_memory_store_missing")
        return PreferenceStore.from_document(
            self.creator_id,
            raw_store,
            deletion_fences=DeletionFenceRegistry(),
        )

    @staticmethod
    def _scope(raw: object) -> PreferenceScope:
        if raw is None:
            return PreferenceScope()
        if not isinstance(raw, dict):
            raise ValueError("preference_scope_invalid")
        allowed = {"output_mode", "content_kind", "language"}
        if set(raw) - allowed:
            raise ValueError("preference_scope_invalid")
        return PreferenceScope(**raw)

    def _proposals(self, store: PreferenceStore) -> list[dict[str, object]]:
        unique: set[tuple[str, PreferenceScope]] = {
            (event.category, event.scope)
            for event in store.events
            if event.creator_id == self.creator_id and event.kind == "episode"
        }
        proposals: list[dict[str, object]] = []
        for category, scope in sorted(unique, key=lambda item: (item[0], repr(item[1]))):
            proposal = store.propose(
                creator_id=self.creator_id,
                category=category,
                scope=scope,
                minimum_examples=2,
            )
            if proposal is None:
                continue
            proposals.append(
                {
                    "proposal_id": proposal_id(proposal),
                    "category": proposal.category,
                    "value": proposal.value,
                    "scope": asdict(proposal.scope),
                    "confidence": proposal.confidence,
                    "evidence_event_ids": list(proposal.evidence_event_ids),
                }
            )
        return proposals

    def inspect(self) -> dict[str, object]:
        with exclusive_lock(self.lock_path):
            document = self._load_document()
            store = self._load_store(document)
            external_provenance: dict[str, list[str]] = {}
            for audit_event in document.get("audit") or []:
                if not isinstance(audit_event, dict):
                    continue
                result_id = str(audit_event.get("result_id") or "")
                evidence_id = str(audit_event.get("source_evidence_event_id") or "")
                if result_id and evidence_id:
                    external_provenance.setdefault(result_id, []).append(evidence_id)
            preferences = [
                {
                    "id": item.preference_id,
                    "category": item.category,
                    "value": item.value,
                    "scope": asdict(item.scope),
                    "status": item.status,
                    "explicit": item.explicit,
                    "confidence": item.confidence,
                    "version": item.version,
                    "provenance_event_ids": list(
                        dict.fromkeys(
                            [
                                *item.provenance_event_ids,
                                *(
                                    evidence_id
                                    for event_id in item.provenance_event_ids
                                    for evidence_id in external_provenance.get(event_id, [])
                                ),
                            ]
                        )
                    ),
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in store.inspect(self.creator_id, include_disabled=True)
            ]
            return {
                "creator_id": self.creator_id,
                "revision": int(document.get("revision", 0)),
                "preferences": preferences,
                "proposals": self._proposals(store),
                "tombstones": [asdict(item) for item in store.tombstones],
                "principle": "Memory is a behavioral prior, never source evidence.",
            }

    def mutate(self, request: dict[str, object]) -> dict[str, object]:
        action = str(request.get("action") or "").strip()
        statement = str(request.get("creator_statement") or "").strip()
        if not action:
            raise ValueError("memory_action_required")
        if not statement:
            raise ValueError("explicit_creator_statement_required")
        with exclusive_lock(self.lock_path):
            document = self._load_document()
            revision = int(document.get("revision", 0))
            expected = request.get("expected_revision")
            if expected is not None and int(expected) != revision:
                raise ValueError("memory_revision_conflict")
            store = self._load_store(document)
            result: object
            if action in {"remember", "episode"}:
                category = str(request.get("category") or "").strip()
                value = str(request.get("value") or "").strip()
                if not category or not value:
                    raise ValueError("memory_category_and_value_required")
                result = store.record_feedback(
                    creator_id=self.creator_id,
                    project_id=str(request.get("project_id") or "desktop-project"),
                    category=category,
                    value=value,
                    scope=self._scope(request.get("scope")),
                    remember=action == "remember",
                )
            elif action == "confirm":
                requested_id = str(request.get("proposal_id") or "")
                match: PreferenceProposal | None = None
                for raw in self._proposals(store):
                    if raw["proposal_id"] != requested_id:
                        continue
                    match = store.propose(
                        creator_id=self.creator_id,
                        category=str(raw["category"]),
                        scope=self._scope(raw["scope"]),
                        minimum_examples=2,
                    )
                    break
                if match is None:
                    raise ValueError("preference_proposal_not_found")
                result = store.confirm(match)
            elif action == "edit":
                result = store.edit(
                    str(request.get("preference_id") or ""),
                    value=(
                        str(request["value"])
                        if request.get("value") is not None
                        else None
                    ),
                    scope=(
                        self._scope(request.get("scope"))
                        if request.get("scope") is not None
                        else None
                    ),
                )
            elif action in {"disable", "enable"}:
                result = store.set_enabled(
                    str(request.get("preference_id") or ""), action == "enable"
                )
            elif action == "delete":
                result = store.delete(str(request.get("preference_id") or ""))
            else:
                raise ValueError("memory_action_unsupported")
            audit = list(document.get("audit") or [])
            audit.append(
                {
                    "event_type": f"memory_{action}",
                    "actor": self.creator_id,
                    "creator_statement": statement,
                    "result_id": getattr(
                        result,
                        "preference_id",
                        getattr(result, "event_id", getattr(result, "deletion_receipt_id", None)),
                    ),
                    "source_evidence_event_id": str(
                        request.get("source_evidence_event_id") or ""
                    )
                    or None,
                    "at": utc_now(),
                }
            )
            document.update(
                {
                    "revision": revision + 1,
                    "store": store.to_document(self.creator_id),
                    "audit": audit,
                    "updated_at": utc_now(),
                }
            )
            atomic_write_json(self.path, document)
        return self.inspect()


def record_review_action(workspace: Path | str, request: dict[str, object]) -> dict[str, object]:
    """Persist an explicit creator review decision without implying publication."""

    action = str(request.get("action") or "").strip()
    if action not in {"approve", "reject", "revise"}:
        raise ValueError("review_action_invalid")
    output_id = str(request.get("output_id") or "").strip()
    statement = str(request.get("creator_statement") or "").strip()
    if not output_id or not statement:
        raise ValueError("review_output_and_statement_required")
    root = Path(workspace).expanduser().resolve() / ".reelbrain" / "desktop"
    path = root / "review-events.jsonl"
    lock_path = root / "review-events.lock"
    event = {
        "event_id": f"review_{sha256(f'{output_id}:{action}:{utc_now()}'.encode()).hexdigest()[:24]}",
        "event_type": "creator_review_action",
        "creator_id": str(request.get("creator_id") or "creator-founder"),
        "project_id": str(request.get("project_id") or "desktop-project"),
        "output_id": output_id,
        "action": action,
        "creator_statement": statement,
        "resulting_state": "CREATOR_REVIEW",
        "publish_ready": False,
        "at": utc_now(),
    }
    with exclusive_lock(lock_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return event


def inspect_review_actions(workspace: Path | str) -> list[dict[str, object]]:
    path = Path(workspace).expanduser().resolve() / ".reelbrain" / "desktop" / "review-events.jsonl"
    if not path.is_file():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return events
