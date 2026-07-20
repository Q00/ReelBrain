"""ReelBrain developer/dogfood command line interface."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform
import shutil
import sys

from .evidence import ReleaseEvidenceStore
from .editing import LocalPackageBuilder, RightsEntry, TranscriptSegment
from .release import CohortFeedback, FounderDogfoodRun, SemanticFixtureResult
from .planning import LongFormPlanBuilder
from .setup import SetupManager
from .transcription import LocalWhisperSTT, SubtitleFileSTT


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


def setup_runtime(args) -> int:
    manager = SetupManager()
    plan = manager.plan()
    if not args.approve:
        print(
            json.dumps(
                {"status": "approval_required", "plan": asdict(plan)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        receipt = manager.apply(approved=True)
    except RuntimeError as exc:
        print(
            json.dumps(
                {"status": "incomplete", "reason": str(exc), "plan": asdict(plan)},
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps({"status": "configured", "receipt": str(receipt)}, indent=2))
    return 0


def evidence_store(args) -> ReleaseEvidenceStore:
    return ReleaseEvidenceStore(args.evidence_dir)


def creator_rights(source: Path, *, license_id: str, modes: tuple[str, ...]) -> tuple[RightsEntry, ...]:
    return (
        RightsEntry(
            asset_id=f"source:{source.name}",
            source="creator-supplied",
            status="approved",
            license_id=license_id,
            permitted_uses=modes,
        ),
    )


def build_short(args) -> int:
    source = args.source.expanduser().resolve()
    stt_provider = (
        SubtitleFileSTT(args.transcript)
        if args.transcript is not None
        else LocalWhisperSTT(model=args.whisper_model, language=args.language)
    )
    package = LocalPackageBuilder().build_short_from_video(
        source=source,
        stt_provider=stt_provider,
        output_dir=args.output,
        project_id=args.project_id,
        creator_id=args.creator_id,
        rights=creator_rights(
            source, license_id=args.rights_license, modes=("short_form_export",)
        ),
        creator_approval_receipt=args.approval_receipt,
        preferred_terms=args.preferred_term,
        approved_thumbnail=args.thumbnail,
    )
    audit = json.loads(package.audit_report.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "status": audit["status"],
                "package": str(package.root),
                "videos": [str(path) for path in package.videos],
            },
            indent=2,
        )
    )
    return 0


def plan_long(args) -> int:
    artifacts = LongFormPlanBuilder().propose(
        source=args.source,
        transcript_provider=SubtitleFileSTT(args.transcript),
        output_dir=args.output,
        project_id=args.project_id,
        creator_id=args.creator_id,
        preferred_terms=args.preferred_term,
    )
    print(
        json.dumps(
            {
                "status": "CREATOR_CONFIRMATION_REQUIRED",
                "artifacts": {name: str(path) for name, path in artifacts.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_long(args) -> int:
    source = args.source.expanduser().resolve()
    argument_rows = json.loads(args.argument_map.read_text(encoding="utf-8"))
    segments = tuple(TranscriptSegment(**row) for row in argument_rows)
    cost_receipt = (
        json.loads(args.cost_receipt.read_text(encoding="utf-8"))
        if args.cost_receipt
        else {"currency": "USD", "reserved": 0, "actual": 0, "mode": "local"}
    )
    package = LocalPackageBuilder().build_long_package(
        source=source,
        argument_map=segments,
        output_dir=args.output,
        project_id=args.project_id,
        creator_id=args.creator_id,
        rights=creator_rights(
            source, license_id=args.rights_license, modes=("long_form_export",)
        ),
        corrected_transcript=args.corrected_transcript.read_text(encoding="utf-8"),
        creator_approval_receipt=args.approval_receipt,
        cost_receipt=cost_receipt,
    )
    print(json.dumps({"status": "PUBLISH_READY", "package": str(package.root), "video": str(package.videos[0])}, indent=2))
    return 0


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


def release_verify_fixtures(args) -> int:
    results = evidence_store(args).verify_required_fixtures(working_dir=Path.cwd())
    print(
        json.dumps(
            {
                "passed": all(result.passed for result in results),
                "fixtures": [
                    {
                        "fixture_id": result.fixture_id,
                        "passed": result.passed,
                        "evidence_ref": result.evidence_ref,
                    }
                    for result in results
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if all(result.passed for result in results) else 2


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

    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve toolbox bootstrap and local conformance; never installs missing packages.",
    )
    setup_parser.set_defaults(func=setup_runtime)

    short = commands.add_parser("short")
    short.add_argument("source", type=Path)
    short.add_argument("--output", type=Path, required=True)
    short.add_argument("--project-id", required=True)
    short.add_argument("--creator-id", required=True)
    short.add_argument(
        "--approval-receipt",
        default="",
        help="Required only to advance a validated package from CREATOR_REVIEW to PUBLISH_READY.",
    )
    short.add_argument("--rights-license", required=True)
    short.add_argument("--whisper-model", default="base")
    short.add_argument("--language")
    short.add_argument(
        "--transcript",
        type=Path,
        help="Optional creator-supplied SRT/VTT; bypasses local Whisper without cloud fallback.",
    )
    short.add_argument("--preferred-term", action="append", default=[])
    short.add_argument("--thumbnail", action=argparse.BooleanOptionalAction, default=False)
    short.set_defaults(func=build_short)

    long_plan = commands.add_parser("plan-long")
    long_plan.add_argument("source", type=Path)
    long_plan.add_argument("--transcript", type=Path, required=True)
    long_plan.add_argument("--output", type=Path, required=True)
    long_plan.add_argument("--project-id", required=True)
    long_plan.add_argument("--creator-id", required=True)
    long_plan.add_argument("--preferred-term", action="append", default=[])
    long_plan.set_defaults(func=plan_long)

    long = commands.add_parser("long")
    long.add_argument("source", type=Path)
    long.add_argument("--output", type=Path, required=True)
    long.add_argument("--project-id", required=True)
    long.add_argument("--creator-id", required=True)
    long.add_argument("--approval-receipt", required=True)
    long.add_argument("--rights-license", required=True)
    long.add_argument("--argument-map", type=Path, required=True)
    long.add_argument("--corrected-transcript", type=Path, required=True)
    long.add_argument("--cost-receipt", type=Path)
    long.set_defaults(func=build_long)

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

    verify_fixtures = release_commands.add_parser("verify-fixtures")
    verify_fixtures.add_argument(
        "--evidence-dir", type=Path, default=default_evidence_dir()
    )
    verify_fixtures.set_defaults(func=release_verify_fixtures)

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
