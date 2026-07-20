import pytest


@pytest.fixture(autouse=True)
def isolated_reelbrain_home(monkeypatch, tmp_path):
    monkeypatch.setenv("REELBRAIN_HOME", str(tmp_path / ".ReelBrain"))
