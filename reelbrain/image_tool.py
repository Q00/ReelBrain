"""Consent-gated OpenAI GPT Image 2 asset generation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from .runtime_guard import RuntimeGuard


class ImageTransport(Protocol):
    def generate(self, *, api_key: str, payload: dict[str, object]) -> dict[str, object]: ...


class OpenAIHTTPImageTransport:
    endpoint = "https://api.openai.com/v1/images/generations"

    def generate(self, *, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=120) as response:  # noqa: S310 - fixed endpoint
            return json.loads(response.read().decode("utf-8"))


class MacOSKeychainSecretResolver:
    """Resolve an opaque Keychain reference inside the provider boundary."""

    def __init__(
        self,
        guard: RuntimeGuard,
        *,
        service: str = "ReelBrain",
        account: str = "openai",
    ) -> None:
        self.guard = guard
        self.service = service
        self.account = account

    def __call__(self, secret_ref: str) -> str:
        if secret_ref != "keychain://ReelBrain/openai":
            raise PermissionError("unapproved_secret_reference")
        result = self.guard.run_tool(
            [
                "security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                self.account,
                "-w",
            ]
        )
        secret = result.stdout.strip()
        if not secret:
            raise RuntimeError("keychain_secret_empty")
        return secret


@dataclass(frozen=True)
class GeneratedImageArtifact:
    image_path: Path
    provenance_path: Path
    model: str
    provider: str


class GPTImage2Tool:
    name = "openai-gpt-image-2"
    official = True
    provider = "openai"

    def __init__(self, transport: ImageTransport | None = None) -> None:
        self.transport = transport or OpenAIHTTPImageTransport()

    def generate_thumbnail(
        self,
        *,
        prompt: str,
        output_path: Path | str,
        guard: RuntimeGuard,
        provider_consent_receipt: dict[str, object],
        secret_resolver: Callable[[str], str],
        secret_ref: str = "keychain://ReelBrain/openai",
        creator_approval_receipt: str,
        size: str = "1536x1024",
        quality: str = "high",
    ) -> GeneratedImageArtifact:
        if not creator_approval_receipt.strip():
            raise ValueError("creator_image_approval_required")
        if not prompt.strip():
            raise ValueError("image_prompt_required")
        destination = Path(output_path).expanduser().resolve()
        guard.authorize_path(destination, operation="write", data_class="generated_image")

        def dispatch() -> dict[str, object]:
            api_key = secret_resolver(secret_ref)
            return self.transport.generate(
                api_key=api_key,
                payload={
                    "model": "gpt-image-2",
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "output_format": "png",
                },
            )

        response = guard.run_callback_tool(
            tool_id=self.name,
            capability="image:generate",
            dispatch=dispatch,
            official=True,
            provider=self.provider,
            consent_receipt=provider_consent_receipt,
        )
        data = response.get("data", [])
        if not data or not data[0].get("b64_json"):
            raise RuntimeError("gpt_image_response_missing_image")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(data[0]["b64_json"]))
        provenance = destination.with_suffix(destination.suffix + ".provenance.json")
        guard.authorize_path(provenance, operation="write", data_class="asset_provenance")
        provenance.write_text(
            json.dumps(
                {
                    "provider": "openai",
                    "model": "gpt-image-2",
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "creator_approval_receipt": creator_approval_receipt,
                    "secret_ref": secret_ref,
                    "synthetic_media_review_required": True,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return GeneratedImageArtifact(destination, provenance, "gpt-image-2", "openai")
