import json
import platform

import pytest

from reelbrain.setup import SetupManager


def test_setup_plan_never_executes_install_commands_and_discloses_missing_dependencies(tmp_path):
    installed = {"ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe"}
    manager = SetupManager(home=tmp_path / ".ReelBrain", which=installed.get)

    plan = manager.plan()

    assert plan.executes_install_commands is False
    whisper = next(item for item in plan.dependencies if item.name == "whisper")
    assert whisper.installed is False
    assert whisper.proposed_command == "uv tool install openai-whisper"
    assert plan.network_destinations == ()


def test_setup_apply_requires_explicit_approval(tmp_path):
    manager = SetupManager(home=tmp_path / ".ReelBrain")

    with pytest.raises(PermissionError, match="explicit_setup_approval_required"):
        manager.apply(approved=False)


def test_setup_fails_closed_when_required_dependency_is_missing(tmp_path):
    manager = SetupManager(home=tmp_path / ".ReelBrain", which=lambda name: None)

    with pytest.raises(RuntimeError, match="required_dependencies_missing:ffmpeg,ffprobe"):
        manager.apply(approved=True)


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64",
    reason="certified macOS Apple Silicon conformance",
)
def test_approved_setup_bootstraps_toolbox_and_writes_conformance_receipt(tmp_path):
    manager = SetupManager(home=tmp_path / ".ReelBrain")

    receipt = manager.apply(approved=True)
    document = json.loads(receipt.read_text())

    assert document["approved"] is True
    assert document["conformance"] == {"ffmpeg": True, "ffprobe": True}
    assert document["install_commands_executed"] == []
    assert (tmp_path / ".ReelBrain" / "toolbox" / "registry.json").is_file()
