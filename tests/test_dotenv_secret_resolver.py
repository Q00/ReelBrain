import pytest

from reelbrain.secrets import DotEnvSecretResolver


def test_dotenv_resolver_maps_legacy_key_only_inside_secret_boundary(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPEN_API_KEY='super-secret'\n", encoding="utf-8")
    resolver = DotEnvSecretResolver(env)

    assert resolver(DotEnvSecretResolver.secret_ref) == "super-secret"
    assert "super-secret" not in repr(resolver)


def test_dotenv_resolver_prefers_standard_variable_and_rejects_other_refs(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "OPEN_API_KEY=legacy\nexport OPENAI_API_KEY=standard\n", encoding="utf-8"
    )
    resolver = DotEnvSecretResolver(env)

    assert resolver(DotEnvSecretResolver.secret_ref) == "standard"
    with pytest.raises(PermissionError, match="unapproved_secret_reference"):
        resolver("dotenv://another/key")


def test_dotenv_resolver_never_falls_back_to_process_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# intentionally empty\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    resolver = DotEnvSecretResolver(env)

    with pytest.raises(RuntimeError, match="openai_api_key_missing_from_dotenv"):
        resolver(DotEnvSecretResolver.secret_ref)
