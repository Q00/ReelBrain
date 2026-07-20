"""Digest-bound ACP toolbox storage and human-gated custom tool lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import platform
import shutil
from typing import Callable, Literal, Mapping

ToolOrigin = Literal["official", "custom", "generated"]
ToolState = Literal["approved", "quarantined", "disabled", "revoked"]


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    version: str
    digest: str
    origin: ToolOrigin
    entrypoint: str
    capabilities: tuple[str, ...]
    supported_platforms: tuple[str, ...] = ("Darwin-arm64",)
    dependencies: tuple[str, ...] = ()
    signature: str | None = None
    signer: str | None = None
    state: ToolState = "quarantined"
    approval_receipt_id: str | None = None
    auditor_report_digest: str | None = None

    def signed_payload(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "digest": self.digest,
            "origin": self.origin,
            "entrypoint": self.entrypoint,
            "capabilities": self.capabilities,
            "supported_platforms": self.supported_platforms,
            "dependencies": self.dependencies,
        }


class ManifestSigner:
    def __init__(self, *, key_id: str, key: bytes) -> None:
        if not key:
            raise ValueError("manifest_signing_key_required")
        self.key_id = key_id
        self._key = key

    def sign(self, manifest: ToolManifest) -> ToolManifest:
        signature = hmac.new(self._key, canonical_json(manifest.signed_payload()), sha256).hexdigest()
        return replace(manifest, signature=signature, signer=self.key_id)

    def verify(self, manifest: ToolManifest) -> bool:
        if manifest.signature is None or manifest.signer != self.key_id:
            return False
        expected = hmac.new(
            self._key, canonical_json(manifest.signed_payload()), sha256
        ).hexdigest()
        return hmac.compare_digest(expected, manifest.signature)


@dataclass(frozen=True)
class ToolRecord:
    manifest: ToolManifest
    artifact_path: Path
    manifest_path: Path


class ToolboxManager:
    """ACP registry is authoritative; filesystem paths are immutable artifacts."""

    def __init__(self, root: Path | str | None = None) -> None:
        home = Path(os.environ.get("REELBRAIN_HOME", "~/.ReelBrain")).expanduser()
        self.root = Path(root).expanduser().resolve() if root else (home / "toolbox").resolve()
        self.official = self.root / "official"
        self.custom = self.root / "custom"
        self.quarantine = self.root / "quarantine"
        self.disabled = self.root / "disabled"
        self.registry_path = self.root / "registry.json"
        for directory in (self.official, self.custom, self.quarantine, self.disabled):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({"active": {}, "records": {}, "revocations": []})

    def install_official(
        self,
        artifact: Path | str,
        manifest: ToolManifest,
        *,
        signer: ManifestSigner,
        conformance: Callable[[Path, ToolManifest], bool],
    ) -> ToolRecord:
        source = Path(artifact).resolve()
        if manifest.origin != "official":
            raise ValueError("official_origin_required")
        if not signer.verify(manifest):
            raise ValueError("official_manifest_signature_invalid")
        self._validate_artifact(source, manifest)
        self._validate_platform(manifest)
        destination = self._immutable_destination(self.official, manifest)
        record = self._copy_immutable(source, destination, replace(manifest, state="approved"))
        if not conformance(record.artifact_path, record.manifest):
            raise ValueError("official_tool_conformance_failed")
        self._activate(record)
        return record

    def stage_generated(
        self,
        request_id: str,
        artifact: Path | str,
        manifest: ToolManifest,
    ) -> ToolRecord:
        if manifest.origin not in {"generated", "custom"}:
            raise ValueError("generated_or_custom_origin_required")
        source = Path(artifact).resolve()
        self._validate_artifact(source, manifest)
        destination = self.quarantine / request_id
        if destination.exists():
            raise FileExistsError("quarantine_request_already_exists")
        return self._copy_immutable(source, destination, replace(manifest, state="quarantined"))

    def approve_custom(
        self,
        request_id: str,
        *,
        human_approver_id: str,
        approval_receipt_id: str,
        auditor_report: Mapping[str, object],
    ) -> ToolRecord:
        if not human_approver_id.startswith("human:"):
            raise ValueError("human_approval_required")
        if not approval_receipt_id.strip():
            raise ValueError("approval_receipt_required")
        if auditor_report.get("passed") is not True:
            raise ValueError("tool_auditor_pass_required")
        staged = self._read_record(self.quarantine / request_id)
        report_digest = sha256(canonical_json(auditor_report)).hexdigest()
        approved_manifest = replace(
            staged.manifest,
            origin="custom",
            state="approved",
            approval_receipt_id=approval_receipt_id,
            auditor_report_digest=report_digest,
        )
        destination = self._immutable_destination(self.custom, approved_manifest)
        record = self._copy_immutable(staged.artifact_path, destination, approved_manifest)
        self._activate(record)
        return record

    def revoke(self, tool_id: str, *, reason: str) -> None:
        registry = self._read_registry()
        active = registry["active"].pop(tool_id, None)
        if active is None:
            raise KeyError("active_tool_not_found")
        record = registry["records"][active]
        record["state"] = "revoked"
        registry["revocations"].append(
            {
                "tool_id": tool_id,
                "record_key": active,
                "reason": reason,
                "artifact_content_retained_immutable": True,
            }
        )
        self._write_registry(registry)
        tombstone = self.disabled / f"{tool_id}.revoked.json"
        tombstone.write_text(
            json.dumps({"tool_id": tool_id, "reason": reason}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def resolve_active(self, tool_id: str) -> ToolRecord:
        registry = self._read_registry()
        key = registry["active"].get(tool_id)
        if key is None:
            raise KeyError("active_tool_not_found")
        raw = registry["records"][key]
        if raw["state"] != "approved":
            raise PermissionError("tool_not_approved")
        return ToolRecord(
            manifest=ToolManifest(**raw["manifest"]),
            artifact_path=Path(raw["artifact_path"]),
            manifest_path=Path(raw["manifest_path"]),
        )

    def find_equivalent(self, capabilities: tuple[str, ...]) -> ToolRecord | None:
        requested = set(capabilities)
        registry = self._read_registry()
        for tool_id in sorted(registry["active"]):
            record = self.resolve_active(tool_id)
            if requested.issubset(record.manifest.capabilities):
                return record
        return None

    def _activate(self, record: ToolRecord) -> None:
        registry = self._read_registry()
        key = self._record_key(record.manifest)
        registry["records"][key] = {
            "state": record.manifest.state,
            "manifest": asdict(record.manifest),
            "artifact_path": str(record.artifact_path),
            "manifest_path": str(record.manifest_path),
        }
        registry["active"][record.manifest.tool_id] = key
        self._write_registry(registry)

    def _copy_immutable(
        self, source: Path, destination: Path, manifest: ToolManifest
    ) -> ToolRecord:
        destination.mkdir(parents=True, exist_ok=False)
        artifact_path = destination / "artifact"
        shutil.copy2(source, artifact_path)
        artifact_path.chmod(0o555 if os.access(source, os.X_OK) else 0o444)
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(
            json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest_path.chmod(0o444)
        return ToolRecord(manifest, artifact_path, manifest_path)

    def _read_record(self, directory: Path) -> ToolRecord:
        manifest_path = directory / "manifest.json"
        artifact_path = directory / "artifact"
        if not manifest_path.is_file() or not artifact_path.is_file():
            raise FileNotFoundError("quarantine_record_incomplete")
        return ToolRecord(
            ToolManifest(**json.loads(manifest_path.read_text(encoding="utf-8"))),
            artifact_path,
            manifest_path,
        )

    def _immutable_destination(self, namespace: Path, manifest: ToolManifest) -> Path:
        return namespace / manifest.tool_id / manifest.version / manifest.digest

    @staticmethod
    def _record_key(manifest: ToolManifest) -> str:
        return f"{manifest.tool_id}:{manifest.version}:{manifest.digest}"

    @staticmethod
    def _validate_artifact(source: Path, manifest: ToolManifest) -> None:
        if not source.is_file():
            raise FileNotFoundError("tool_artifact_missing")
        if sha256_file(source) != manifest.digest:
            raise ValueError("tool_artifact_digest_mismatch")
        if not manifest.tool_id.strip() or not manifest.version.strip():
            raise ValueError("tool_identity_required")
        if not manifest.capabilities:
            raise ValueError("tool_capabilities_required")
        if any(marker in manifest.entrypoint.lower() for marker in ("api_key=", "token=", "secret=")):
            raise ValueError("tool_manifest_secret_material_denied")

    @staticmethod
    def _validate_platform(manifest: ToolManifest) -> None:
        current = f"{platform.system()}-{platform.machine()}"
        if current not in manifest.supported_platforms:
            raise ValueError(f"tool_platform_unsupported:{current}")

    def _read_registry(self) -> dict[str, object]:
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def _write_registry(self, registry: dict[str, object]) -> None:
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.registry_path)

