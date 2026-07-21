import pytest

from reelbrain.cli import _usd_to_cents, build_parser


def test_dogfood_prepare_and_provider_approval_are_explicit_subcommands(tmp_path):
    parser = build_parser()
    prepare = parser.parse_args(
        [
            "dogfood",
            "prepare",
            str(tmp_path / "videos"),
            "--output",
            str(tmp_path / "run"),
            "--project-id",
            "founder-run",
            "--creator-id",
            "founder",
        ]
    )
    approve = parser.parse_args(
        [
            "dogfood",
            "approve-provider",
            str(tmp_path / "plan.json"),
            "--receipt",
            "creator-approved",
            "--cap-usd",
            "15.00",
        ]
    )
    run = parser.parse_args(
        [
            "dogfood",
            "run",
            str(tmp_path / "videos"),
            "--output",
            str(tmp_path / "run"),
            "--project-id",
            "founder-run",
            "--creator-id",
            "founder",
            "--provider-plan",
            str(tmp_path / "run" / "provider_plan.json"),
            "--env-file",
            str(tmp_path / ".env"),
            "--image-approval-receipt",
            "founder-requested-gpt-image-2",
        ]
    )

    assert prepare.shorts == 3
    assert prepare.minimum_long_minutes == 10
    assert approve.cap_usd == "15.00"
    assert _usd_to_cents(approve.cap_usd) == 1500
    assert run.shorts == 3
    assert run.image_approval_receipt == "founder-requested-gpt-image-2"


def test_usd_cap_rejects_fractional_cents():
    with pytest.raises(ValueError, match="exact_cents"):
        _usd_to_cents("15.001")
