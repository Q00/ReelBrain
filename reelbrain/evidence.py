"""Durable, append-only release evidence collection."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import platform

from .release import (
    CohortFeedback,
    FounderDogfoodRun,
    ReleaseBar,
    ReleaseEvidence,
    SemanticFixtureResult,
)


class ReleaseEvidenceStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "release_evidence.jsonl"

    def append(self, event_type: str, payload: dict[str, object]) -> None:
        event = {"type": event_type, "payload": payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")

    def record_governance_run(self, *, passed: bool, receipt: str) -> None:
        self.append("governance_run", {"passed": passed, "receipt": receipt})

    def record_fixture(self, fixture: SemanticFixtureResult) -> None:
        self.append("semantic_fixture", asdict(fixture))

    def record_founder_run(self, run: FounderDogfoodRun) -> None:
        self.append("founder_run", asdict(run))

    def record_cohort_feedback(self, feedback: CohortFeedback) -> None:
        self.append("cohort_feedback", asdict(feedback))

    def load(self) -> ReleaseEvidence:
        governance_clean_runs = 0
        fixtures: list[SemanticFixtureResult] = []
        founder_runs: list[FounderDogfoodRun] = []
        cohort: list[CohortFeedback] = []
        if self.events_path.is_file():
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                payload = event["payload"]
                match event["type"]:
                    case "governance_run":
                        governance_clean_runs += int(bool(payload["passed"]))
                    case "semantic_fixture":
                        fixtures.append(SemanticFixtureResult(**payload))
                    case "founder_run":
                        founder_runs.append(FounderDogfoodRun(**payload))
                    case "cohort_feedback":
                        cohort.append(CohortFeedback(**payload))
        return ReleaseEvidence(
            platform="macOS" if platform.system() == "Darwin" else platform.system(),
            architecture=platform.machine(),
            governance_clean_runs=governance_clean_runs,
            fixtures=tuple(fixtures),
            founder_runs=tuple(founder_runs),
            cohort=tuple(cohort),
        )

    def evaluate_and_write(self) -> dict[str, Path]:
        return ReleaseBar().write_reports(self.load(), self.root / "reports")

