"""Secret resolvers that keep raw credentials inside provider boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

class DotEnvSecretResolver:
    """Resolve one OpenAI key from a creator-owned ``.env`` file.

    The resolver deliberately exposes only an opaque reference to callers.  It
    is intended as a dogfood bridge for existing projects; durable production
    configuration should migrate the same key into the OS keychain or an
    encrypted vault.
    """

    secret_ref = "dotenv://ReelBrain/openai"
    store_id = "reelbrain-project-dotenv"
    store_kind = "local_dotenv_ephemeral"
    store_source = "project-owned .env"

    def __init__(
        self,
        path: Path | str,
        *,
        variable_names: Iterable[str] = ("OPENAI_API_KEY", "OPEN_API_KEY"),
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        self._variable_names = tuple(variable_names)

    def __call__(self, secret_ref: str) -> str:
        if secret_ref != self.secret_ref:
            raise PermissionError("unapproved_secret_reference")
        # RuntimeGuard authorizes this opaque secret reference immediately
        # before invoking the resolver. Secret-bearing files intentionally use
        # that dedicated secret-store path instead of ordinary local-data
        # receipts, which correctly reject direct secret reads.
        if not self._path.is_file():
            raise RuntimeError("dotenv_secret_store_missing")
        values = self._parse(self._path.read_text(encoding="utf-8"))
        for variable_name in self._variable_names:
            value = values.get(variable_name, "").strip()
            if value:
                return value
        raise RuntimeError("openai_api_key_missing_from_dotenv")

    @staticmethod
    def _parse(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
        return values

    def __repr__(self) -> str:
        return "DotEnvSecretResolver(<redacted>)"
