"""ReelBrain developer/dogfood command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import sys

from .evidence import ReleaseEvidenceStore
from .release import CohortFeedback, FounderDogfoodRun, SemanticFixtureResult


def default_evidence_dir() -> Path:
    return Path.cwd() / ".reelbrain" / "release-evidence"


def doctor() -> int:
    checks = {
        "platform": platform.system() == "Darwin" and platform.machine() == "arm64",
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "whisper_optional": shutil.which("whisper") is not None,
    }
    payload = {
        "certified_v1": checks["platform"] and checks["ffmpeg"] and checks["ffprobe"],
        "checks": checks,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "note": "Whisper is optional at doctor time and required only for LocalWhisperSTT runs.",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["certified_v1"] else 1


def evidence_store(args) -> ReleaseEvidenceStore:
    return ReleaseEvidenceStore(args.evidence_dir)


def release_evaluate(args) -> int:
    store = evidence_store(args)
    reports = store.evaluate_and_write()
    verdict = json.loads(reports["release_verification_report"].read_text())
    print(json.dumps({"passed": verdict["passed"], "failed_checks": verdict["failed_checks"], "reports": {key: str(path) for key, path in reports.items()}}, indent=2, sort_keys=True))
    return 0 if verdict["passed"] else 2


def release_governance(args) -> int:
    evidence_store(args).record_governance_run(passed=args.passed, receipt=args.receipt)
    return 0


def release_fixture(args) -> int:
    evidence_store(args).record_fixture(
        SemanticFixtureResult(
            fixture_id=args.fixture_id,
            passed=args.passed,
            must_pass=args.must_pass,
            first_pass=not args.repaired,
            major_defect=args.major_defect,
            critical_failure=args.critical_failure,
            slice_name=args.slice,
        )
    )
    return 0


def release_founder(args) -> int:
    evidence_store(args).record_founder_run(
        FounderDogfoodRun(
            run_id=args.run_id,
            output_mode=args.output_mode,
            state=args.state,
            objective_gates_passed=args.objective_gates_passed,
            critical_failure=args.critical_failure,
        )
    )
    return 0


def release_cohort(args) -> int:
    evidence_store(args).record_cohort_feedback(
        CohortFeedback(
            creator_id=args.creator_id,
            approves_fidelity_and_personalization=args.approves,
            willing_to_publish=args.willing_to_publish,
            minor_revisions=args.minor_revisions,
            objective_gates_passed=args.objective_gates_passed,
            critical_failure=args.critical_failure,
        )
    )
    return 0


def boolean_flags(parser: argparse.ArgumentParser, name: str, *, default: bool = False) -> None:
    parser.add_argument(f"--{name}", action=argparse.BooleanOptionalAction, default=default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reelbrain")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.set_defaults(func=lambda args: doctor())

    release = commands.add_parser("release")
    release_commands = release.add_subparsers(dest="release_command", required=True)

    evaluate = release_commands.add_parser("evaluate")
    evaluate.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    evaluate.set_defaults(func=release_evaluate)

    governance = release_commands.add_parser("record-governance")
    governance.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    boolean_flags(governance, "passed", default=True)
    governance.add_argument("--receipt", required=True)
    governance.set_defaults(func=release_governance)

    fixture = release_commands.add_parser("record-fixture")
    fixture.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    fixture.add_argument("--fixture-id", required=True)
    boolean_flags(fixture, "passed", default=True)
    boolean_flags(fixture, "must-pass")
    boolean_flags(fixture, "repaired")
    boolean_flags(fixture, "major-defect")
    boolean_flags(fixture, "critical-failure")
    fixture.add_argument("--slice", default="default")
    fixture.set_defaults(func=release_fixture)

    founder = release_commands.add_parser("record-founder")
    founder.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    founder.add_argument("--run-id", required=True)
    founder.add_argument("--output-mode", choices=("short", "long"), required=True)
    founder.add_argument("--state", default="PUBLISH_READY")
    boolean_flags(founder, "objective-gates-passed", default=True)
    boolean_flags(founder, "critical-failure")
    founder.set_defaults(func=release_founder)

    cohort = release_commands.add_parser("record-cohort")
    cohort.add_argument("--evidence-dir", type=Path, default=default_evidence_dir())
    cohort.add_argument("--creator-id", required=True)
    boolean_flags(cohort, "approves")
    boolean_flags(cohort, "willing-to-publish")
    cohort.add_argument("--minor-revisions", type=int, default=0)
    boolean_flags(cohort, "objective-gates-passed", default=True)
    boolean_flags(cohort, "critical-failure")
    cohort.set_defaults(func=release_cohort)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

