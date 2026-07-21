"""Founder dogfood orchestration for real multi-video ReelBrain runs.

This module keeps every stage inspectable. Provider-backed stages are added to
the run only after an exact disclosure/budget plan has been creator-approved;
renders remain ``CREATOR_REVIEW`` until the founder supplies output approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Callable, Iterable
from zipfile import ZipFile, ZipInfo

from .editing import MediaError, probe_media
from .provider_plan import (
    ProviderAuthorizationPlan,
    load_provider_authorization_plan,
)
from .transcription import BilingualTranscript, TranscriptChunk


SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
MAX_ARCHIVE_MEMBERS = 1000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024**3
MAX_ARCHIVE_ENTRY_BYTES = 50 * 1024**3
MAX_ARCHIVE_COMPRESSION_RATIO = 200


@dataclass(frozen=True)
class SourceAsset:
    source_id: str
    path: Path
    original_name: str
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class DogfoodRunConfig:
    project_id: str
    creator_id: str
    output_root: Path
    shorts_per_source: int = 3
    minimum_long_seconds: float = 600.0
    maximum_long_seconds: float = 900.0
    rights_license: str = "creator-owned"

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.creator_id.strip():
            raise ValueError("dogfood_project_and_creator_required")
        if self.project_id != self.project_id.strip() or self.creator_id != self.creator_id.strip():
            raise ValueError("dogfood_identity_fields_must_be_canonical")
        if not 2 <= self.shorts_per_source <= 10:
            raise ValueError("dogfood_shorts_must_be_2_to_10")
        if not 600 <= self.minimum_long_seconds <= self.maximum_long_seconds <= 900:
            raise ValueError("dogfood_long_form_must_be_10_to_15_minutes")
        if not self.rights_license.strip():
            raise ValueError("dogfood_rights_license_required")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_zip_member(member: ZipInfo, destination: Path) -> Path:
    member_path = Path(member.filename)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise MediaError("unsafe_zip_member_path")
    # Unix symlinks store the file type in the upper mode bits.
    if (member.external_attr >> 16) & 0o170000 == 0o120000:
        raise MediaError("zip_symlink_not_allowed")
    resolved = (destination / member_path).resolve()
    if destination.resolve() not in (resolved, *resolved.parents):
        raise MediaError("unsafe_zip_member_path")
    return resolved


def extract_video_archive(archive: Path | str, destination: Path | str) -> tuple[Path, ...]:
    archive_path = Path(archive).expanduser().resolve()
    root = Path(destination).expanduser().resolve()
    if not archive_path.is_file():
        raise MediaError("video_archive_missing")
    root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with ZipFile(archive_path) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise MediaError("video_archive_member_limit_exceeded")
        total_uncompressed = 0
        normalized_targets: set[str] = set()
        for member in members:
            target = _safe_zip_member(member, root)
            normalized = str(target.relative_to(root)).casefold()
            if normalized in normalized_targets:
                raise MediaError("video_archive_duplicate_target")
            normalized_targets.add(normalized)
            if member.flag_bits & 0x1:
                raise MediaError("encrypted_zip_member_not_allowed")
            if member.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                raise MediaError("video_archive_entry_too_large")
            total_uncompressed += member.file_size
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise MediaError("video_archive_uncompressed_size_limit_exceeded")
            if total_uncompressed > shutil.disk_usage(root).free * 0.9:
                raise MediaError("insufficient_disk_space_for_video_archive")
            if member.file_size and (
                member.compress_size == 0
                or member.file_size / member.compress_size
                > MAX_ARCHIVE_COMPRESSION_RATIO
            ):
                raise MediaError("video_archive_compression_ratio_limit_exceeded")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.partial")
            with bundle.open(member) as source_handle, temporary.open("wb") as output_handle:
                shutil.copyfileobj(source_handle, output_handle, length=1024 * 1024)
            if temporary.stat().st_size != member.file_size:
                temporary.unlink(missing_ok=True)
                raise MediaError("video_archive_entry_size_mismatch")
            temporary.replace(target)
            extracted.append(target)
    if not extracted:
        raise MediaError("video_archive_contains_no_supported_sources")
    return tuple(sorted(extracted, key=lambda path: path.name))


def discover_video_sources(input_path: Path | str, extraction_root: Path | str) -> tuple[Path, ...]:
    source = Path(input_path).expanduser().resolve()
    if source.suffix.lower() == ".zip":
        return extract_video_archive(source, extraction_root)
    if source.is_dir():
        unique: dict[Path, Path] = {}
        for path in source.iterdir():
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
                unique.setdefault(path.resolve(), path.resolve())
        videos = tuple(sorted(unique.values(), key=lambda path: path.name))
    elif source.is_file() and source.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
        videos = (source,)
    else:
        raise MediaError("unsupported_dogfood_input")
    if not videos:
        raise MediaError("dogfood_input_contains_no_supported_sources")
    return videos


def inventory_video_sources(paths: Iterable[Path | str]) -> tuple[SourceAsset, ...]:
    assets: list[SourceAsset] = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path).expanduser().resolve()
        info = probe_media(path)
        video = info.video_stream
        audio = info.audio_stream
        if video is None or audio is None or video.width is None or video.height is None:
            raise MediaError("dogfood_source_requires_decodable_audio_and_video")
        assets.append(
            SourceAsset(
                source_id=f"source-{index:02d}",
                path=path,
                original_name=path.name,
                duration_seconds=info.duration_seconds,
                width=video.width,
                height=video.height,
                video_codec=video.codec_name,
                audio_codec=audio.codec_name,
                bytes=path.stat().st_size,
                sha256=_file_sha256(path),
            )
        )
    return tuple(assets)


def prepare_dogfood_run(
    *,
    input_path: Path | str,
    config: DogfoodRunConfig,
) -> dict[str, Path]:
    root = config.output_root.expanduser().resolve()
    sources = discover_video_sources(input_path, root / "input")
    inventory = inventory_video_sources(sources)
    root.mkdir(parents=True, exist_ok=True)
    from .runtime_guard import RuntimeGuard
    from .transcription import FFmpegSpeechWindowDetector

    detected_windows: dict[str, tuple[object, ...]] = {}
    for asset in inventory:
        guard = RuntimeGuard(
            workspace_root=root / "preflight" / asset.source_id,
            local_allowlist=(asset.path.parent,),
            project_id=f"{config.project_id}-preflight-{asset.source_id}",
            creator_id=config.creator_id,
            agent_id="meaning-scout",
            tool_names=("ffmpeg", "ffprobe"),
        )
        detected_windows[asset.source_id] = FFmpegSpeechWindowDetector(
            minimum_silence_seconds=30
        ).detect(asset.path, guard)
    inventory_path = root / "source_inventory.json"
    inventory_path.write_text(
        json.dumps(
            [
                {
                    **asdict(asset),
                    "path": str(asset.path),
                    "speech_windows": [
                        asdict(window) for window in detected_windows[asset.source_id]
                    ],
                    "active_speech_seconds": sum(
                        window.duration for window in detected_windows[asset.source_id]
                    ),
                }
                for asset in inventory
            ],
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    provider_plan = ProviderAuthorizationPlan.founder_dogfood(
        project_id=config.project_id,
        creator_id=config.creator_id,
        source_count=len(inventory),
        shorts_per_source=config.shorts_per_source,
        source_asset_digests=tuple(asset.sha256 for asset in inventory),
        source_active_seconds=tuple(
            sum(window.duration for window in detected_windows[asset.source_id])
            for asset in inventory
        ),
    )
    provider_path = provider_plan.write(root / "provider_authorization_plan.json")
    run_manifest = root / "run_manifest.json"
    run_manifest.write_text(
        json.dumps(
            {
                "status": "AWAITING_PROVIDER_APPROVAL",
                "project_id": config.project_id,
                "creator_id": config.creator_id,
                "source_count": len(inventory),
                "shorts_per_source": config.shorts_per_source,
                "long_form_duration_seconds": [
                    config.minimum_long_seconds,
                    config.maximum_long_seconds,
                ],
                "rights_license": config.rights_license,
                "provider_plan": str(provider_path),
                "source_inventory": str(inventory_path),
                "creator_review_required": True,
                "durable_preference_change": False,
                "preference_note": (
                    "The requested blur/title/bilingual layout is current-run steering, "
                    "not durable memory, because the creator did not ask to remember it."
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "source_inventory": inventory_path,
        "provider_plan": provider_path,
        "run_manifest": run_manifest,
    }


def _partition(items: tuple[object, ...], count: int) -> tuple[tuple[object, ...], ...]:
    if count < 1 or count > len(items):
        raise ValueError("invalid_partition_count")
    groups: list[tuple[object, ...]] = []
    for index in range(count):
        start = round(index * len(items) / count)
        end = round((index + 1) * len(items) / count)
        groups.append(items[start:end])
    return tuple(groups)


def _split_text_to_limit(text: str, limit: int) -> tuple[str, ...]:
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("caption_text_required")
    words = normalized.split(" ")
    parts: list[str] = []
    current = ""
    for word in words:
        if len(word) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.extend(word[index : index + limit] for index in range(0, len(word), limit))
            continue
        proposed = word if not current else f"{current} {word}"
        if len(proposed) <= limit:
            current = proposed
        else:
            parts.append(current)
            current = word
    if current:
        parts.append(current)
    return tuple(parts)


def _split_text_into_count(text: str, count: int, limit: int) -> tuple[str, ...]:
    parts = list(_split_text_to_limit(text, limit))
    if len(parts) >= count:
        return tuple(parts)
    while len(parts) < count:
        split_index = max(range(len(parts)), key=lambda index: len(parts[index]))
        candidate = parts[split_index]
        words = candidate.split()
        if len(words) >= 2:
            midpoint = len(words) // 2
            left = " ".join(words[:midpoint])
            right = " ".join(words[midpoint:])
        elif len(candidate) >= 2:
            midpoint = len(candidate) // 2
            left = candidate[:midpoint].strip()
            right = candidate[midpoint:].strip()
        else:
            return tuple(parts)
        if not left or not right:
            return tuple(parts)
        parts[split_index : split_index + 1] = [left, right]
    return tuple(parts)


def bilingual_caption_cues_for_range(
    transcript: BilingualTranscript,
    *,
    source_start: float,
    source_end: float,
    output_offset: float = 0.0,
):
    """Align Korean/English tracks and produce strict two-line render cues."""

    from .dogfood_render import BilingualCaptionCue

    if source_start < 0 or source_end <= source_start or output_offset < 0:
        raise ValueError("invalid_bilingual_caption_range")
    korean = tuple(
        chunk
        for chunk in transcript.korean
        if chunk.start < source_end and chunk.end > source_start
    )
    english = tuple(
        chunk
        for chunk in transcript.english
        if chunk.start < source_end and chunk.end > source_start
    )
    if not korean or not english:
        raise MediaError("bilingual_caption_track_missing_for_selected_range")
    if len(korean) != len(english):
        raise MediaError("bilingual_caption_tracks_not_canonically_aligned")
    cues: list[BilingualCaptionCue] = []
    previous_end = output_offset
    for ko_chunk, en_chunk in zip(korean, english):
        if (
            abs(ko_chunk.start - en_chunk.start) > 0.001
            or abs(ko_chunk.end - en_chunk.end) > 0.001
        ):
            raise MediaError("bilingual_caption_tracks_not_canonically_aligned")
        source_cue_start = max(source_start, ko_chunk.start)
        source_cue_end = min(source_end, ko_chunk.end)
        cue_start = max(previous_end, output_offset + source_cue_start - source_start)
        cue_end = output_offset + source_cue_end - source_start
        if cue_end <= cue_start + 0.05:
            continue
        korean_text = ko_chunk.text.strip()
        english_text = en_chunk.text.strip()
        ko_parts = _split_text_to_limit(korean_text, 42)
        en_parts = _split_text_to_limit(english_text, 64)
        part_count = max(len(ko_parts), len(en_parts))
        for _ in range(1000):
            ko_parts = _split_text_into_count(korean_text, part_count, 42)
            en_parts = _split_text_into_count(english_text, part_count, 64)
            next_count = max(len(ko_parts), len(en_parts))
            if len(ko_parts) == len(en_parts) == part_count:
                break
            if next_count <= part_count:
                raise MediaError("bilingual_caption_segmentation_failed")
            part_count = next_count
        else:
            raise MediaError("bilingual_caption_segmentation_failed")
        if len(ko_parts) != len(en_parts):
            raise MediaError("bilingual_caption_segmentation_failed")
        part_duration = (cue_end - cue_start) / len(ko_parts)
        for part_index, (ko_text, en_text) in enumerate(zip(ko_parts, en_parts)):
            start = cue_start + part_index * part_duration
            end = cue_end if part_index == len(ko_parts) - 1 else start + part_duration
            cues.append(BilingualCaptionCue(start, end, ko_text, en_text))
        previous_end = cue_end
    if not cues:
        raise MediaError("bilingual_caption_cues_empty")
    return tuple(cues)


def write_bilingual_transcript(
    transcript: BilingualTranscript, destination: Path | str
) -> Path:
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(transcript), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_bilingual_transcript(path: Path | str) -> BilingualTranscript:
    document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    from .transcription import SpeechWindow

    return BilingualTranscript(
        korean=tuple(TranscriptChunk(**row) for row in document["korean"]),
        english=tuple(TranscriptChunk(**row) for row in document["english"]),
        speech_windows=tuple(SpeechWindow(**row) for row in document["speech_windows"]),
        original_language=document.get("original_language", "ko"),
        translation_language=document.get("translation_language", "en"),
        provider=document.get("provider", "openai"),
        model=document.get("model", "whisper-1"),
    )


def write_editorial_plan(plan, destination: Path | str) -> Path:
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(plan), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_editorial_plan(path: Path | str):
    from .editorial import (
        EditorialPlan,
        EditorialTraceRecord,
        LongFormDraftPlan,
        LongFormSection,
        PersonaCandidateSelection,
        PersonaLaneResult,
        ShortDraft,
    )

    document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return EditorialPlan(
        shorts=tuple(ShortDraft(**row) for row in document["shorts"]),
        long_form=LongFormDraftPlan(
            **{
                **document["long_form"],
                "sections": tuple(
                    LongFormSection(**row)
                    for row in document["long_form"].get("sections", [])
                ),
            }
        ),
        persona_selections=tuple(
            PersonaLaneResult(
                persona=row["persona"],
                selections=tuple(
                    PersonaCandidateSelection(**selection)
                    for selection in row["selections"]
                ),
            )
            for row in document["persona_selections"]
        ),
        trace=tuple(EditorialTraceRecord(**row) for row in document["trace"]),
    )


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


def _stage_receipt_matches(
    *,
    artifact: Path,
    receipt: Path,
    expected_scope: dict[str, object],
) -> bool:
    if not artifact.is_file() or not receipt.is_file():
        return False
    try:
        document = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        document.get("scope") == expected_scope
        and document.get("artifact_sha256") == _file_sha256(artifact)
    )


def _write_stage_receipt(
    *, artifact: Path, receipt: Path, scope: dict[str, object]
) -> Path:
    return _write_json(
        receipt,
        {
            "scope": scope,
            "artifact": str(artifact),
            "artifact_sha256": _file_sha256(artifact),
        },
    )


def _load_completed_source_package(
    package_path: Path,
    *,
    asset: SourceAsset,
    config: DogfoodRunConfig,
) -> list[dict[str, object]] | None:
    if not package_path.is_file():
        return None
    try:
        document = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("status") != "CREATOR_REVIEW":
        return None
    source = document.get("source")
    outputs = document.get("outputs")
    if (
        document.get("project_id") != config.project_id
        or document.get("creator_id") != config.creator_id
        or not isinstance(source, dict)
        or source.get("sha256") != asset.sha256
        or not isinstance(outputs, list)
        or len(outputs) != config.shorts_per_source + 1
    ):
        return None
    if sum(isinstance(row, dict) and row.get("mode") == "short" for row in outputs) != config.shorts_per_source:
        return None
    if sum(isinstance(row, dict) and row.get("mode") == "long" for row in outputs) != 1:
        return None
    artifact_hashes: dict[str, str] = {}
    required_paths = (
        "video",
        "captions_ko",
        "captions_en",
        "captions_ass",
        "thumbnail",
        "thumbnail_background",
        "thumbnail_provenance",
        "thumbnail_provider_receipt",
    )
    normalized_outputs: list[dict[str, object]] = []
    for row in outputs:
        if not isinstance(row, dict) or row.get("status") != "CREATOR_REVIEW":
            return None
        try:
            duration = float(row["duration_seconds"])
        except (KeyError, TypeError, ValueError):
            return None
        if row.get("mode") == "short" and not 30 <= duration <= 60:
            return None
        if row.get("mode") == "long" and not (
            config.minimum_long_seconds <= duration <= config.maximum_long_seconds
        ):
            return None
        for key in required_paths:
            raw_path = row.get(key)
            if not isinstance(raw_path, str):
                return None
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size == 0:
                return None
            artifact_hashes[f"{row.get('output_id')}:{key}"] = _file_sha256(path)
        normalized_outputs.append(dict(row))
    receipt = package_path.with_suffix(".receipt.json")
    scope = {
        "stage": "completed_source_package",
        "source_sha256": asset.sha256,
        "shorts_per_source": config.shorts_per_source,
        "minimum_long_seconds": config.minimum_long_seconds,
        "maximum_long_seconds": config.maximum_long_seconds,
        "artifact_hashes": artifact_hashes,
    }
    if receipt.is_file() and not _stage_receipt_matches(
        artifact=package_path,
        receipt=receipt,
        expected_scope=scope,
    ):
        return None
    if not receipt.is_file():
        _write_stage_receipt(artifact=package_path, receipt=receipt, scope=scope)
    return normalized_outputs


def _hydrate_guard_audit(guard, audit_root: Path) -> None:
    from .governance import ACPToolIdentity
    from .governance import ACPRegistrySnapshot

    list_fields = {
        "capability_receipts": "capability_receipts.json",
        "provider_receipts": "provider_receipts.json",
        "budget_ledger": "budget_ledger.json",
        "denial_logs": "denial_logs.json",
        "approval_records": "approval_records.json",
    }
    for attribute, filename in list_fields.items():
        path = audit_root / filename
        if not path.is_file():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rows, list):
            getattr(guard, attribute).extend(rows)
    registry_path = audit_root / "acp_registry.json"
    if registry_path.is_file():
        try:
            rows = json.loads(registry_path.read_text(encoding="utf-8"))
            for row in rows:
                identity = ACPToolIdentity(**row)
                guard._executed_tools[identity.tool_id] = identity
            guard.registry = ACPRegistrySnapshot(tuple(guard._executed_tools.values()))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass


def _latest_reservation_state(guard, reservation_id: str) -> str | None:
    return next(
        (
            str(row.get("state"))
            for row in reversed(guard.budget_ledger)
            if row.get("reservation_id") == reservation_id
        ),
        None,
    )


def _thumbnail_prompt(*, title: str, transcript_text: str, orientation: str) -> str:
    aspect = "vertical 9:16" if orientation == "vertical" else "horizontal 16:9"
    excerpt = " ".join(transcript_text.split())[:800]
    return (
        f"Create a polished {aspect} background for an educational AI creator video. "
        f"Topic: {title}. Grounding excerpt: {excerpt}. Visual language: modern "
        "open-source agent systems, connected workflow nodes, subtle recursive loop "
        "motif, deep charcoal with electric teal and warm orange highlights, high "
        "contrast, premium editorial lighting. Leave clean negative space in the "
        "center for a locally overlaid exact title. No people, no faces, no words, "
        "no letters, no logos, no watermarks, no copyrighted characters."
    )


class FounderDogfoodRunner:
    """Execute the approved four-video founder run with resumable stage artifacts."""

    def __init__(
        self,
        *,
        transcriber=None,
        editorial_team=None,
        image_tool=None,
        renderer_factory: Callable[..., object] | None = None,
        agent_executor_factory: Callable[..., object] | None = None,
        allow_test_adapters: bool = False,
    ) -> None:
        self.transcriber = transcriber
        self.editorial_team = editorial_team
        self.image_tool = image_tool
        self.renderer_factory = renderer_factory
        self.agent_executor_factory = agent_executor_factory
        self.allow_test_adapters = allow_test_adapters

    def run(
        self,
        *,
        sources: Iterable[Path | str],
        config: DogfoodRunConfig,
        provider_plan_path: Path | str,
        env_file: Path | str,
        image_approval_receipt: str,
    ) -> dict[str, Path]:
        from .agent_tools import AgentToolExecutor
        from .dogfood_render import DogfoodRenderer, RenderSegment
        from .editorial import EditorialAgentTeam, OpenAIResponsesHTTPTransport
        from .image_tool import GPTImage2Tool, OpenAIHTTPImageTransport
        from .runtime_guard import RuntimeGuard
        from .secrets import DotEnvSecretResolver
        from .transcription import OpenAIWhisperHTTPTransport, OpenAIWhisperSTT

        if not image_approval_receipt.strip():
            raise ValueError("dogfood_image_approval_receipt_required")
        provider_plan = load_provider_authorization_plan(provider_plan_path)
        provider_plan.require_approved()
        if (
            provider_plan.project_id != config.project_id
            or provider_plan.creator_id != config.creator_id
        ):
            raise PermissionError("provider_plan_scope_mismatch")
        assets = inventory_video_sources(sources)
        planned_sources = {
            call.call_id.rsplit(":", 1)[-1]
            for call in provider_plan.calls
            if call.call_id.startswith(f"stt:{config.project_id}:")
        }
        if planned_sources != {asset.source_id for asset in assets}:
            raise PermissionError("provider_plan_source_set_mismatch")
        if tuple(asset.sha256 for asset in assets) != provider_plan.source_asset_digests:
            raise PermissionError("provider_plan_source_digest_mismatch")

        root = config.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        env_path = Path(env_file).expanduser().resolve()
        resolver = DotEnvSecretResolver(env_path)
        transcriber = self.transcriber or OpenAIWhisperSTT()
        editorial_team = self.editorial_team or EditorialAgentTeam(
            OpenAIResponsesHTTPTransport()
        )
        image_tool = self.image_tool or GPTImage2Tool()
        renderer_factory = self.renderer_factory or DogfoodRenderer
        executor_factory = self.agent_executor_factory or AgentToolExecutor
        if not self.allow_test_adapters:
            approved_adapter_shape = (
                type(transcriber) is OpenAIWhisperSTT
                and type(transcriber.transport) is OpenAIWhisperHTTPTransport
                and type(editorial_team) is EditorialAgentTeam
                and type(editorial_team.transport) is OpenAIResponsesHTTPTransport
                and type(image_tool) is GPTImage2Tool
                and type(image_tool.transport) is OpenAIHTTPImageTransport
                and renderer_factory is DogfoodRenderer
            )
            if not approved_adapter_shape:
                raise PermissionError("unapproved_dogfood_adapter_injection")
        executor_kwargs = {
            "project_id": config.project_id,
            "creator_id": config.creator_id,
            "workspace_root": root,
            "read_roots": tuple(asset.path.parent for asset in assets) + (env_path.parent,),
        }
        if self.allow_test_adapters and self.agent_executor_factory is None:
            from .toolbox import ToolboxManager

            executor_kwargs["toolbox"] = ToolboxManager(root / "governance" / "toolbox")
        agent_executor = executor_factory(**executor_kwargs)
        agent_contract_path = agent_executor.write_execution_contract(
            root / "governance" / "agent_execution_contract.json"
        )
        agent_trace_path = root / "governance" / "agent_tool_trace.json"
        package_paths: list[Path] = []
        all_outputs: list[dict[str, object]] = []

        for asset in assets:
            source_root = root / asset.source_id
            source_root.mkdir(parents=True, exist_ok=True)
            existing_package = source_root / "package_manifest.json"
            completed_outputs = _load_completed_source_package(
                existing_package,
                asset=asset,
                config=config,
            )
            if completed_outputs is not None:
                package_paths.append(existing_package)
                all_outputs.extend(completed_outputs)
                continue
            rights = [
                {
                    "asset_id": f"source:{asset.sha256}",
                    "source": "creator-supplied",
                    "status": "approved",
                    "license_id": config.rights_license,
                    "permitted_uses": ["short_form_export", "long_form_export"],
                }
            ]
            provider_audit_root = source_root / "governance" / "provider"
            provider_guard = RuntimeGuard(
                workspace_root=source_root,
                local_allowlist=(asset.path.parent, env_path.parent),
                project_id=config.project_id,
                creator_id=config.creator_id,
                agent_id="showrunner",
                tool_names=("ffmpeg", "ffprobe"),
            )
            _hydrate_guard_audit(provider_guard, provider_audit_root)
            transcript_path = source_root / "bilingual_transcript.json"
            transcript_receipt = source_root / "bilingual_transcript.receipt.json"
            transcript_call = provider_plan.call(
                f"stt:{config.project_id}:{asset.source_id}"
            )
            transcript_scope = {
                "stage": "bilingual_transcription",
                "source_sha256": asset.sha256,
                "provider_scope_digest": provider_plan.scope_digest(),
                "provider_call_id": transcript_call.call_id,
                "tool_id": transcript_call.tool_id,
                "model": getattr(transcriber, "model", "unknown"),
                "chunk_seconds": getattr(transcriber, "chunk_seconds", None),
                "overlap_seconds": getattr(transcriber, "overlap_seconds", None),
            }
            if _stage_receipt_matches(
                artifact=transcript_path,
                receipt=transcript_receipt,
                expected_scope=transcript_scope,
            ):
                transcript = load_bilingual_transcript(transcript_path)
            else:
                transcript_budget = transcript_call.budget_receipt(
                    project_id=config.project_id,
                    creator_id=config.creator_id,
                    approval_receipt_id=provider_plan.approval_receipt_id,
                )
                if _latest_reservation_state(
                    provider_guard, str(transcript_budget["reservation_id"])
                ) == "consumed":
                    raise PermissionError(
                        "consumed_transcription_reservation_without_valid_artifact"
                    )
                try:
                    transcript = agent_executor.invoke(
                        agent="meaning-scout",
                        tool_id="transcribe-bilingual",
                        payload={
                            "source_id": asset.source_id,
                            "source_path": str(asset.path),
                            "provider_call_id": transcript_call.call_id,
                        },
                        dispatch=lambda: transcriber.transcribe_bilingual(
                            asset.path,
                            guard=provider_guard,
                            provider_consent_receipt=transcript_call.consent_receipt(
                                project_id=config.project_id,
                                creator_id=config.creator_id,
                                approval_receipt_id=provider_plan.approval_receipt_id,
                            ),
                            budget_reservation_receipt=transcript_budget,
                            secret_resolver=resolver,
                            secret_ref=resolver.secret_ref,
                            secret_store_id=resolver.store_id,
                            secret_store_kind=resolver.store_kind,
                            secret_store_source=resolver.store_source,
                            checkpoint_dir=source_root / "provider-checkpoints" / "stt",
                            checkpoint_scope=sha256(
                                json.dumps(
                                    transcript_scope,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        ),
                    )
                finally:
                    provider_guard.write_audit_artifacts(
                        provider_audit_root, rights_manifest=rights
                    )
                write_bilingual_transcript(transcript, transcript_path)
                _write_stage_receipt(
                    artifact=transcript_path,
                    receipt=transcript_receipt,
                    scope=transcript_scope,
                )

            editorial_path = source_root / "editorial_plan.json"
            editorial_receipt = source_root / "editorial_plan.receipt.json"
            editorial_call = provider_plan.call(
                f"editorial:{config.project_id}:{asset.source_id}"
            )
            editorial_scope = {
                "stage": "editorial_plan",
                "source_sha256": asset.sha256,
                "transcript_sha256": _file_sha256(transcript_path),
                "provider_scope_digest": provider_plan.scope_digest(),
                "provider_call_id": editorial_call.call_id,
                "tool_id": editorial_call.tool_id,
                "model": getattr(
                    getattr(editorial_team, "transport", None), "model", "fixture"
                ),
                "short_count": config.shorts_per_source,
                "minimum_long_seconds": config.minimum_long_seconds,
                "maximum_long_seconds": config.maximum_long_seconds,
            }
            if _stage_receipt_matches(
                artifact=editorial_path,
                receipt=editorial_receipt,
                expected_scope=editorial_scope,
            ):
                editorial_plan = load_editorial_plan(editorial_path)
            else:
                editorial_budget = editorial_call.budget_receipt(
                    project_id=config.project_id,
                    creator_id=config.creator_id,
                    approval_receipt_id=provider_plan.approval_receipt_id,
                )
                if _latest_reservation_state(
                    provider_guard, str(editorial_budget["reservation_id"])
                ) == "consumed":
                    raise PermissionError(
                        "consumed_editorial_reservation_without_valid_artifact"
                    )
                try:
                    editorial_plan = agent_executor.invoke(
                        agent="showrunner",
                        tool_id="plan-editorial-candidates",
                        payload={
                            "source_id": asset.source_id,
                            "transcript_path": str(transcript_path),
                            "short_count": config.shorts_per_source,
                        },
                        dispatch=lambda: editorial_team.plan(
                            transcript.korean,
                            creator_preferences=(
                                "self-contained educational value",
                                "complete thoughts with natural endings",
                                "current-run steering: centered full source over blurred vertical background",
                                "current-run steering: exact Korean and English captions",
                            ),
                            short_count=config.shorts_per_source,
                            minimum_long_seconds=config.minimum_long_seconds,
                            maximum_long_seconds=config.maximum_long_seconds,
                            guard=provider_guard,
                            provider_consent_receipt=editorial_call.consent_receipt(
                                project_id=config.project_id,
                                creator_id=config.creator_id,
                                approval_receipt_id=provider_plan.approval_receipt_id,
                            ),
                            budget_reservation_receipt=editorial_budget,
                            secret_resolver=resolver,
                            secret_ref=resolver.secret_ref,
                            secret_store_id=resolver.store_id,
                            secret_store_kind=resolver.store_kind,
                            secret_store_source=resolver.store_source,
                            checkpoint_dir=source_root
                            / "provider-checkpoints"
                            / "editorial",
                            checkpoint_scope=sha256(
                                json.dumps(
                                    editorial_scope,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        ),
                    )
                finally:
                    provider_guard.write_audit_artifacts(
                        provider_audit_root, rights_manifest=rights
                    )
                write_editorial_plan(editorial_plan, editorial_path)
                _write_stage_receipt(
                    artifact=editorial_path,
                    receipt=editorial_receipt,
                    scope=editorial_scope,
                )
            if len(editorial_plan.shorts) != config.shorts_per_source:
                raise MediaError("editorial_short_count_outside_requested_configuration")
            caption_quality_report = _write_json(
                source_root / "caption_quality_report.json",
                {
                    "status": "UNVERIFIED_CREATOR_CORRECTION_REQUIRED",
                    "required_word_accuracy": 0.95,
                    "measured_word_accuracy": None,
                    "reason": (
                        "Provider confidence/no-speech probability is not an independent "
                        "word-error-rate reference. Creator correction or an independent "
                        "reference transcript is required before PUBLISH_READY."
                    ),
                    "low_confidence_ranges": [
                        {
                            "chunk_id": chunk.chunk_id,
                            "start": chunk.start,
                            "end": chunk.end,
                            "confidence": chunk.confidence,
                        }
                        for chunk in transcript.korean
                        if chunk.confidence < 0.9
                    ],
                },
            )

            renderer = renderer_factory(
                source_root,
                project_id=config.project_id,
                creator_id=config.creator_id,
                read_roots=(asset.path.parent, source_root),
                output_fps=30,
                ffmpeg_preset="veryfast",
            )
            output_rows: list[dict[str, object]] = []
            for output_index, short in enumerate(editorial_plan.shorts, start=1):
                video = source_root / "shorts" / f"short_{output_index:02d}.mp4"
                captions = bilingual_caption_cues_for_range(
                    transcript,
                    source_start=short.start_seconds,
                    source_end=short.end_seconds,
                )
                output_id = f"{asset.source_id}-short-{output_index:02d}"
                rendered = agent_executor.invoke(
                    agent="assembler",
                    tool_id="render-vertical-short",
                    payload={
                        "source_id": asset.source_id,
                        "output_id": output_id,
                        "start_seconds": short.start_seconds,
                        "duration_seconds": short.duration_seconds,
                    },
                    dispatch=lambda: renderer.render_short(
                        source=asset.path,
                        start=short.start_seconds,
                        duration=short.duration_seconds,
                        title=short.angle,
                        captions=captions,
                        output=video,
                    ),
                )
                thumbnail = agent_executor.invoke(
                    agent="thumbnail-designer",
                    tool_id="design-thumbnail",
                    payload={
                        "source_id": asset.source_id,
                        "output_id": output_id,
                        "title": short.angle,
                        "orientation": "vertical",
                    },
                    dispatch=lambda: self._generate_thumbnail(
                        provider_plan=provider_plan,
                        provider_guard=provider_guard,
                        resolver=resolver,
                        image_tool=image_tool,
                        renderer=renderer,
                        config=config,
                        asset=asset,
                        output_index=output_index,
                        title=short.angle,
                        transcript_text=short.text,
                        orientation="vertical",
                        image_approval_receipt=image_approval_receipt,
                        output_dir=video.parent,
                        provider_audit_root=provider_audit_root,
                        rights=rights,
                    ),
                )
                output_rows.append(
                    {
                        "output_id": output_id,
                        "mode": "short",
                        "status": "CREATOR_REVIEW",
                        "title": short.angle,
                        "video": str(rendered.video),
                        "captions_ko": str(rendered.korean_srt),
                        "captions_en": str(rendered.english_srt),
                        "captions_ass": str(rendered.combined_ass),
                        "thumbnail": str(thumbnail.image),
                        "thumbnail_background": str(
                            video.parent
                            / f"thumbnail_{output_index:02d}.gpt-image-2.png"
                        ),
                        "thumbnail_provenance": str(
                            video.parent
                            / f"thumbnail_{output_index:02d}.gpt-image-2.png.provenance.json"
                        ),
                        "thumbnail_provider_receipt": str(
                            video.parent
                            / f"thumbnail_{output_index:02d}.gpt-image-2.png.dogfood-receipt.json"
                        ),
                        "source_range": [short.start_seconds, short.end_seconds],
                        "duration_seconds": rendered.duration_seconds,
                        "rationale": short.rationale,
                        "caption_accuracy_status": "UNVERIFIED_CREATOR_CORRECTION_REQUIRED",
                    }
                )

            long_plan = editorial_plan.long_form
            if not (
                config.minimum_long_seconds
                <= long_plan.duration_seconds
                <= config.maximum_long_seconds
            ):
                raise MediaError("editorial_long_form_outside_requested_bounds")
            long_video = source_root / "long" / "long_form.mp4"
            long_captions = bilingual_caption_cues_for_range(
                transcript,
                source_start=long_plan.start_seconds,
                source_end=long_plan.end_seconds,
            )
            long_output_id = f"{asset.source_id}-long-01"
            rendered_long = agent_executor.invoke(
                agent="assembler",
                tool_id="render-long-form",
                payload={
                    "source_id": asset.source_id,
                    "output_id": long_output_id,
                    "segments": [
                        [long_plan.start_seconds, long_plan.end_seconds]
                    ],
                },
                dispatch=lambda: renderer.render_long(
                    source=asset.path,
                    segments=(
                        RenderSegment(
                            long_plan.start_seconds,
                            long_plan.end_seconds,
                            label=long_plan.title,
                        ),
                    ),
                    captions=long_captions,
                    output=long_video,
                    title=long_plan.title,
                ),
            )
            long_thumbnail = agent_executor.invoke(
                agent="thumbnail-designer",
                tool_id="design-thumbnail",
                payload={
                    "source_id": asset.source_id,
                    "output_id": long_output_id,
                    "title": long_plan.title,
                    "orientation": "horizontal",
                },
                dispatch=lambda: self._generate_thumbnail(
                    provider_plan=provider_plan,
                    provider_guard=provider_guard,
                    resolver=resolver,
                    image_tool=image_tool,
                    renderer=renderer,
                    config=config,
                    asset=asset,
                    output_index=config.shorts_per_source + 1,
                    title=long_plan.title,
                    transcript_text=" ".join(
                        chunk.text
                        for chunk in transcript.korean
                        if chunk.start < long_plan.end_seconds
                        and chunk.end > long_plan.start_seconds
                    ),
                    orientation="horizontal",
                    image_approval_receipt=image_approval_receipt,
                    output_dir=long_video.parent,
                    provider_audit_root=provider_audit_root,
                    rights=rights,
                ),
            )
            output_rows.append(
                {
                    "output_id": long_output_id,
                    "mode": "long",
                    "status": "CREATOR_REVIEW",
                    "title": long_plan.title,
                    "video": str(rendered_long.video),
                    "captions_ko": str(rendered_long.korean_srt),
                    "captions_en": str(rendered_long.english_srt),
                    "captions_ass": str(rendered_long.combined_ass),
                    "thumbnail": str(long_thumbnail.image),
                    "thumbnail_background": str(
                        long_video.parent
                        / f"thumbnail_{config.shorts_per_source + 1:02d}.gpt-image-2.png"
                    ),
                    "thumbnail_provenance": str(
                        long_video.parent
                        / f"thumbnail_{config.shorts_per_source + 1:02d}.gpt-image-2.png.provenance.json"
                    ),
                    "thumbnail_provider_receipt": str(
                        long_video.parent
                        / f"thumbnail_{config.shorts_per_source + 1:02d}.gpt-image-2.png.dogfood-receipt.json"
                    ),
                    "source_range": [long_plan.start_seconds, long_plan.end_seconds],
                    "duration_seconds": rendered_long.duration_seconds,
                    "rationale": long_plan.rationale,
                    "caption_accuracy_status": "UNVERIFIED_CREATOR_CORRECTION_REQUIRED",
                }
            )

            provider_governance = provider_guard.write_audit_artifacts(
                source_root / "governance" / "provider", rights_manifest=rights
            )
            render_governance = renderer.guard.write_audit_artifacts(
                source_root / "governance" / "render", rights_manifest=rights
            )
            agent_executor.write_trace(agent_trace_path)
            package = _write_json(
                source_root / "package_manifest.json",
                {
                    "status": "CREATOR_REVIEW",
                    "project_id": config.project_id,
                    "creator_id": config.creator_id,
                    "source": {**asdict(asset), "path": str(asset.path)},
                    "outputs": output_rows,
                    "transcript": str(transcript_path),
                    "editorial_plan": str(editorial_path),
                    "caption_quality_report": str(caption_quality_report),
                    "provider_governance": {
                        key: str(path) for key, path in provider_governance.items()
                    },
                    "render_governance": {
                        key: str(path) for key, path in render_governance.items()
                    },
                    "agent_execution_contract": str(agent_contract_path),
                    "agent_tool_trace": str(agent_trace_path),
                    "creator_approval_receipt": "",
                    "publish_ready": False,
                },
            )
            package_paths.append(package)
            all_outputs.extend(output_rows)

        agent_executor.write_trace(agent_trace_path)
        manifest = _write_json(
            root / "run_manifest.json",
            {
                "status": "CREATOR_REVIEW",
                "project_id": config.project_id,
                "creator_id": config.creator_id,
                "source_count": len(assets),
                "output_count": len(all_outputs),
                "short_count": sum(row["mode"] == "short" for row in all_outputs),
                "long_count": sum(row["mode"] == "long" for row in all_outputs),
                "provider_plan": str(Path(provider_plan_path).expanduser().resolve()),
                "provider_hard_cap_cents": provider_plan.hard_cap_cents,
                "packages": [str(path) for path in package_paths],
                "outputs": all_outputs,
                "agent_execution_contract": str(agent_contract_path),
                "agent_tool_trace": str(agent_trace_path),
                "creator_review_required": True,
                "publish_ready": False,
            },
        )
        evolution = _write_json(
            root / "evolution_report.json",
            {
                "baseline": {
                    "shorts": "three generic local drafts, black letterboxing, one-language captions",
                    "long_form": "5-12 minute creator-confirmed only",
                    "provider_path": "GPT Image 2 existed but was not wired into CLI",
                },
                "real_world_observations": [
                    "silent lead-ins and 11-13 hour silent tails must not inflate agent context or cost",
                    "installed FFmpeg lacks libass/drawtext, requiring exact-text raster overlays",
                ],
                "current_run_behavior": [
                    "speech-window-aware bilingual Whisper transcription",
                    "four independent editorial persona lanes plus grounded Showrunner",
                    f"{config.shorts_per_source} 30-60 second Shorts per source",
                    "one contiguous 10-15 minute natural-boundary long-form draft per source",
                    "centered full horizontal source over blurred vertical background",
                    "Korean and English sidecars plus visibly burned two-line captions",
                    "one governed GPT Image 2 thumbnail per output",
                    "all outputs stop at CREATOR_REVIEW",
                ],
                "memory": {
                    "applied_as": "current steering",
                    "durable_preference_written": False,
                    "reason": "memory is a behavior prior, not evidence; no remember request was given",
                },
                "evidence": {
                    "run_manifest": str(manifest),
                    "package_manifests": [str(path) for path in package_paths],
                },
            },
        )
        return {"manifest": manifest, "evolution_report": evolution}

    @staticmethod
    def _generate_thumbnail(
        *,
        provider_plan: ProviderAuthorizationPlan,
        provider_guard,
        resolver,
        image_tool,
        renderer,
        config: DogfoodRunConfig,
        asset: SourceAsset,
        output_index: int,
        title: str,
        transcript_text: str,
        orientation: str,
        image_approval_receipt: str,
        output_dir: Path,
        provider_audit_root: Path,
        rights: list[dict[str, object]],
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        background = output_dir / f"thumbnail_{output_index:02d}.gpt-image-2.png"
        prompt = _thumbnail_prompt(
            title=title,
            transcript_text=transcript_text,
            orientation=orientation,
        )
        size = "1088x1920" if orientation == "vertical" else "1920x1088"
        call = provider_plan.call(
            f"image:{config.project_id}:{asset.source_id}:output-{output_index:02d}"
        )
        receipt = background.with_suffix(background.suffix + ".dogfood-receipt.json")
        provenance = background.with_suffix(background.suffix + ".provenance.json")
        image_scope = {
            "stage": "gpt_image_2_thumbnail_background",
            "source_sha256": asset.sha256,
            "provider_scope_digest": provider_plan.scope_digest(),
            "provider_call_id": call.call_id,
            "tool_id": call.tool_id,
            "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
            "size": size,
            "quality": "medium",
            "orientation": orientation,
            "image_generation_authorization_receipt": image_approval_receipt,
        }
        cache_valid = _stage_receipt_matches(
            artifact=background,
            receipt=receipt,
            expected_scope=image_scope,
        )
        if cache_valid:
            try:
                provenance_document = json.loads(provenance.read_text(encoding="utf-8"))
                cache_valid = (
                    provenance_document.get("model") == "gpt-image-2"
                    and provenance_document.get("provider_invocation_id") == call.call_id
                    and provenance_document.get("prompt_sha256")
                    == image_scope["prompt_sha256"]
                    and provenance_document.get("image_sha256")
                    == _file_sha256(background)
                )
            except (OSError, json.JSONDecodeError):
                cache_valid = False
        if not cache_valid:
            image_budget = call.budget_receipt(
                project_id=config.project_id,
                creator_id=config.creator_id,
                approval_receipt_id=provider_plan.approval_receipt_id,
            )
            if _latest_reservation_state(
                provider_guard, str(image_budget["reservation_id"])
            ) == "consumed":
                raise PermissionError(
                    "consumed_image_reservation_without_valid_artifact"
                )
            try:
                image_tool.generate_thumbnail(
                    prompt=prompt,
                    output_path=background,
                    guard=provider_guard,
                    provider_consent_receipt=call.consent_receipt(
                        project_id=config.project_id,
                        creator_id=config.creator_id,
                        approval_receipt_id=provider_plan.approval_receipt_id,
                    ),
                    budget_reservation_receipt=image_budget,
                    secret_resolver=resolver,
                    secret_ref=resolver.secret_ref,
                    secret_store_id=resolver.store_id,
                    secret_store_kind=resolver.store_kind,
                    secret_store_source=resolver.store_source,
                    generation_authorization_receipt=image_approval_receipt,
                    size=size,
                    quality="medium",
                )
            finally:
                provider_guard.write_audit_artifacts(
                    provider_audit_root, rights_manifest=rights
                )
            try:
                provenance_document = json.loads(provenance.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MediaError("gpt_image_2_provenance_missing_or_invalid") from exc
            if not (
                provenance_document.get("model") == "gpt-image-2"
                and provenance_document.get("provider_invocation_id") == call.call_id
                and provenance_document.get("prompt_sha256") == image_scope["prompt_sha256"]
                and provenance_document.get("image_sha256") == _file_sha256(background)
            ):
                raise MediaError("gpt_image_2_provenance_scope_mismatch")
            _write_stage_receipt(
                artifact=background,
                receipt=receipt,
                scope=image_scope,
            )
        thumbnail = output_dir / f"thumbnail_{output_index:02d}.jpg"
        return renderer.render_thumbnail(
            background=background,
            title=title,
            output=thumbnail,
            orientation=orientation,
        )
