import json
import subprocess

import pytest

from reelbrain.editing import MediaError
from reelbrain.runtime_guard import RuntimeGuard
from reelbrain.transcription import (
    FFmpegSpeechWindowDetector,
    OpenAIWhisperHTTPTransport,
    OpenAIWhisperSTT,
    SpeechWindow,
    SubtitleFileSTT,
    TranscriptChunk,
)


def test_creator_supplied_srt_is_parsed_as_local_reference(tmp_path):
    transcript = tmp_path / "source.srt"
    transcript.write_text(
        """1
00:00:10,000 --> 00:00:45,000
Memory is a behavioral prior, not evidence.

2
00:01:00,000 --> 00:01:35,000
ACP translates tools while the broker enforces effects.
""",
        encoding="utf-8",
    )

    provider = SubtitleFileSTT(transcript)
    chunks = provider.transcribe(tmp_path / "unused.mp4")

    assert provider.provider is None
    assert provider.reference_kind == "creator_supplied_transcript"
    assert [(chunk.start, chunk.end) for chunk in chunks] == [(10.0, 45.0), (60.0, 95.0)]
    assert chunks[0].text == "Memory is a behavioral prior, not evidence."


def test_creator_supplied_vtt_supports_minute_and_hour_timestamps(tmp_path):
    transcript = tmp_path / "source.vtt"
    transcript.write_text(
        """WEBVTT

00:10.000 --> 00:45.000
First complete educational thought.

01:00:00.000 --> 01:00:35.000
Second complete educational thought.
""",
        encoding="utf-8",
    )

    chunks = SubtitleFileSTT(transcript).transcribe(tmp_path / "unused.mp4")

    assert [(chunk.start, chunk.end) for chunk in chunks] == [
        (10.0, 45.0),
        (3600.0, 3635.0),
    ]


class FixtureMediaGuard:
    def __init__(self):
        self.commands = []

    def authorize_path(self, *_args, **_kwargs):
        return None

    def run_tool(self, command):
        self.commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "120"},
                        "streams": [
                            {"codec_type": "video", "codec_name": "h264"},
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="""[silencedetect] silence_start: 0
[silencedetect] silence_end: 12.5 | silence_duration: 12.5
[silencedetect] silence_start: 48
[silencedetect] silence_end: 55 | silence_duration: 7
[silencedetect] silence_start: 95
[silencedetect] silence_end: 120 | silence_duration: 25
""",
        )


def test_ffmpeg_speech_detection_trims_only_outer_silence_and_keeps_source_time(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    guard = FixtureMediaGuard()

    windows = FFmpegSpeechWindowDetector().detect(source, guard)

    assert windows == (SpeechWindow(12.5, 95.0),)
    assert [command[0] for command in guard.commands] == ["ffprobe", "ffmpeg"]
    assert "silencedetect=noise=-35dB:duration=1" in guard.commands[1]


class FixtureSpeechDetector:
    def detect(self, source, guard):
        return (SpeechWindow(100.0, 130.0),)


class FixtureAudioExtractor:
    def __init__(self):
        self.ranges = []

    def extract(self, *, source, start, end, destination, guard):
        self.ranges.append((start, end))
        destination.write_bytes(f"audio:{start}:{end}".encode())


class FixtureWhisperTransport:
    def __init__(self):
        self.calls = []
        self.transcription_count = 0
        self.translation_count = 0

    def transcribe(self, *, api_key, audio_path, model, language):
        self.calls.append(("transcribe", api_key, audio_path.name, model, language))
        self.transcription_count += 1
        if self.transcription_count == 1:
            return {
                "segments": [
                    {"start": 0, "end": 10, "text": "첫 번째 설명"},
                    {"start": 15, "end": 20, "text": "경계 문장"},
                ]
            }
        return {
            "segments": [
                {"start": 0, "end": 10, "text": "마지막 설명"},
            ]
        }

    def translate(self, *, api_key, audio_path, model):
        self.calls.append(("translate", api_key, audio_path.name, model, None))
        self.translation_count += 1
        if self.translation_count == 1:
            return {
                "segments": [
                    {"start": 0, "end": 10, "text": "First explanation"},
                    {"start": 15, "end": 20, "text": "Boundary sentence"},
                ]
            }
        return {
            "segments": [
                {"start": 0, "end": 10, "text": "Final explanation"},
            ]
        }


def _openai_stt_consent():
    return {
        "provider": "openai",
        "tool_id": "openai-whisper-1",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "destination": "api.openai.com",
        "invocation_id": "stt-call-1",
        "approval_receipt_id": "provider-consent-stt-1",
        "data_categories": ["audio"],
        "purpose": "Korean transcription and English translation",
        "expected_retention": "provider request lifecycle",
        "expected_cost": "approved Whisper calls",
    }


def _openai_stt_budget():
    return {
        "reservation_id": "budget-stt-1",
        "requester_id": "reelbrain-runtime",
        "session_id": "runtime:project-1",
        "tool_id": "openai-whisper-1",
        "project_id": "project-1",
        "creator_id": "creator-1",
        "capabilities": ["stt:transcribe"],
        "reserved_amount_cents": 20,
        "metered_units": 4,
        "cost_authorization_receipt_id": "cost-approved-stt-1",
        "state": "reserved",
    }


def test_governed_openai_whisper_restores_offsets_builds_bilingual_tracks_and_dedupes(
    tmp_path,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture video")
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    extractor = FixtureAudioExtractor()
    transport = FixtureWhisperTransport()
    resolved_refs = []
    secret = "sk-not-recorded-anywhere"
    provider = OpenAIWhisperSTT(
        transport,
        speech_detector=FixtureSpeechDetector(),
        audio_extractor=extractor,
        chunk_seconds=20,
        overlap_seconds=0,
    )

    transcript = provider.transcribe_bilingual(
        source,
        guard=guard,
        provider_consent_receipt=_openai_stt_consent(),
        budget_reservation_receipt=_openai_stt_budget(),
        secret_resolver=lambda ref: resolved_refs.append(ref) or secret,
    )

    assert provider.official is True
    assert provider.provider == "openai"
    assert provider.destination_host == "api.openai.com"
    assert provider.default_secret_ref.startswith("keychain://")
    assert extractor.ranges == [(100.0, 120.0), (120.0, 130.0)]
    assert [(item.start, item.end, item.text) for item in transcript.korean] == [
        (100.0, 110.0, "첫 번째 설명"),
        (115.0, 120.0, "경계 문장"),
        (120.0, 130.0, "마지막 설명"),
    ]
    assert [(item.start, item.end, item.text) for item in transcript.english] == [
        (100.0, 110.0, "First explanation"),
        (115.0, 120.0, "Boundary sentence"),
        (120.0, 130.0, "Final explanation"),
    ]
    assert transcript.speech_windows == (SpeechWindow(100.0, 130.0),)
    assert resolved_refs == ["keychain://ReelBrain/openai"]
    assert [call[0] for call in transport.calls] == [
        "transcribe",
        "translate",
        "transcribe",
        "translate",
    ]
    assert all(call[1] == secret for call in transport.calls)
    audit = json.dumps(
        {
            "capabilities": guard.capability_receipts,
            "providers": guard.provider_receipts,
            "budget": guard.budget_ledger,
            "approvals": guard.approval_records,
        }
    )
    assert secret not in audit
    assert [row["state"] for row in guard.budget_ledger] == ["reserved", "consumed"]


def test_openai_whisper_denies_missing_consent_before_resolving_secret_or_transport(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture video")
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    extractor = FixtureAudioExtractor()
    transport = FixtureWhisperTransport()
    secret_calls = []

    with pytest.raises(PermissionError, match="provider_consent_required"):
        OpenAIWhisperSTT(
            transport,
            speech_detector=FixtureSpeechDetector(),
            audio_extractor=extractor,
        ).transcribe_bilingual(
            source,
            guard=guard,
            provider_consent_receipt={},
            budget_reservation_receipt=_openai_stt_budget(),
            secret_resolver=lambda ref: secret_calls.append(ref) or "secret",
        )

    assert secret_calls == []
    assert transport.calls == []


def test_openai_http_transport_enforces_25mb_file_limit_without_dispatch(tmp_path):
    oversized = tmp_path / "oversized.mp3"
    with oversized.open("wb") as handle:
        handle.seek(OpenAIWhisperHTTPTransport.maximum_file_bytes)
        handle.write(b"x")

    with pytest.raises(MediaError, match="openai_audio_file_exceeds_25mb"):
        OpenAIWhisperHTTPTransport().transcribe(
            api_key="test-only",
            audio_path=oversized,
            model="whisper-1",
            language="ko",
        )


def test_openai_http_transport_uses_fixed_multipart_transcription_and_translation_endpoints(
    tmp_path, monkeypatch
):
    audio = tmp_path / "chunk.mp3"
    audio.write_bytes(b"audio fixture")
    requests = []

    class FixtureResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"segments": []}'

    def fixture_urlopen(request, timeout):
        requests.append((request, timeout))
        return FixtureResponse()

    monkeypatch.setattr("reelbrain.transcription.urlopen", fixture_urlopen)
    transport = OpenAIWhisperHTTPTransport()

    transport.transcribe(
        api_key="dispatch-only-secret",
        audio_path=audio,
        model="whisper-1",
        language="ko",
    )
    transport.translate(
        api_key="dispatch-only-secret",
        audio_path=audio,
        model="whisper-1",
    )

    assert [item[0].full_url for item in requests] == [
        "https://api.openai.com/v1/audio/transcriptions",
        "https://api.openai.com/v1/audio/translations",
    ]
    assert all(item[1] == 180 for item in requests)
    transcription_body = requests[0][0].data
    translation_body = requests[1][0].data
    assert b'name="file"; filename="chunk.mp3"' in transcription_body
    assert b'name="model"\r\n\r\nwhisper-1' in transcription_body
    assert b'name="language"\r\n\r\nko' in transcription_body
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment' in transcription_body
    assert b'name="language"' not in translation_body
    assert b'name="timestamp_granularities[]"' not in translation_body


def test_boundary_deduplication_fails_closed_on_non_identical_overlap():
    chunks = (
        TranscriptChunk("first", 10, 20, "첫 설명", 0.9),
        TranscriptChunk("overlap", 18, 24, "이어지는 다른 문장", 0.95),
    )

    with pytest.raises(RuntimeError, match="segments_overlap"):
        OpenAIWhisperSTT._deduplicate_boundaries(chunks)


def test_whisper_drops_only_low_confidence_no_speech_tail_outside_chunk():
    response = {
        "segments": [
            {"start": 590, "end": 600, "text": "grounded ending", "no_speech_prob": 0.1},
            {"start": 603, "end": 613, "text": "repeated tail", "no_speech_prob": 0.96},
        ]
    }

    chunks = OpenAIWhisperSTT._response_chunks(response, 100, 600, "ko-1")

    assert [(chunk.start, chunk.end, chunk.text) for chunk in chunks] == [
        (690, 700, "grounded ending")
    ]


def test_whisper_rejects_confident_timestamp_outside_chunk():
    response = {
        "segments": [
            {"start": 603, "end": 613, "text": "claimed speech", "no_speech_prob": 0.1}
        ]
    }

    with pytest.raises(RuntimeError, match="timestamp_out_of_bounds"):
        OpenAIWhisperSTT._response_chunks(response, 0, 600, "ko-1")


def test_bilingual_alignment_merges_different_segmentation_on_canonical_korean_timing():
    korean = (
        TranscriptChunk("ko-1", 0, 2, "첫 문장"),
        TranscriptChunk("ko-2", 2, 4, "둘째 문장"),
    )
    english = (TranscriptChunk("en-1", 0, 4, "First and second sentences."),)

    aligned_ko, aligned_en = OpenAIWhisperSTT._align_bilingual_chunks(korean, english)

    assert [(item.start, item.end) for item in aligned_ko] == [(0, 4)]
    assert [(item.start, item.end) for item in aligned_en] == [(0, 4)]
    assert aligned_ko[0].text == "첫 문장 둘째 문장"
    assert aligned_en[0].text == "First and second sentences."


def test_bilingual_alignment_discards_only_low_confidence_unpaired_silence_hallucination():
    korean = (
        TranscriptChunk("ko-grounded", 0, 4, "근거 문장", 0.95),
        TranscriptChunk("ko-tail", 20, 22, "반복", 0.2),
    )
    english = (TranscriptChunk("en-grounded", 0, 4, "Grounded sentence.", 0.9),)

    aligned_ko, aligned_en = OpenAIWhisperSTT._align_bilingual_chunks(korean, english)

    assert [item.text for item in aligned_ko] == ["근거 문장"]
    assert [item.text for item in aligned_en] == ["Grounded sentence."]


def test_bilingual_alignment_rejects_confident_unpaired_segment():
    korean = (
        TranscriptChunk("ko-grounded", 0, 4, "근거 문장", 0.95),
        TranscriptChunk("ko-orphan", 20, 22, "확신 문장", 0.9),
    )
    english = (TranscriptChunk("en-grounded", 0, 4, "Grounded sentence.", 0.9),)

    with pytest.raises(RuntimeError, match="bilingual_translation_segment_unaligned"):
        OpenAIWhisperSTT._align_bilingual_chunks(korean, english)


def test_bilingual_alignment_attaches_small_provider_timing_gap_without_global_merging():
    korean = (
        TranscriptChunk("ko-1", 0, 4, "첫 문장", 0.95),
        TranscriptChunk("ko-2", 10, 14, "둘째 문장", 0.9),
    )
    english = (
        TranscriptChunk("en-1", 0, 4, "First sentence.", 0.9),
        TranscriptChunk("en-2", 14.3, 18, "Second sentence.", 0.95),
    )

    aligned_ko, aligned_en = OpenAIWhisperSTT._align_bilingual_chunks(korean, english)

    assert len(aligned_ko) == len(aligned_en) == 2
    assert aligned_ko[1].text == "둘째 문장"
    assert aligned_en[1].text == "Second sentence."


def test_provider_checkpoints_prevent_rebilling_successful_subcalls_after_partial_failure(
    tmp_path,
):
    class FlakyTransport:
        def __init__(self):
            self.calls = []
            self.failed = False

        def transcribe(self, *, api_key, audio_path, model, language):
            self.calls.append(("transcribe", audio_path.read_bytes()))
            return {"segments": [{"start": 0, "end": 10, "text": "한국어 문장"}]}

        def translate(self, *, api_key, audio_path, model):
            self.calls.append(("translate", audio_path.read_bytes()))
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary_provider_failure")
            return {"segments": [{"start": 0, "end": 10, "text": "English sentence"}]}

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture video")
    guard = RuntimeGuard(
        workspace_root=tmp_path,
        project_id="project-1",
        creator_id="creator-1",
        tool_names=(),
    )
    transport = FlakyTransport()
    provider = OpenAIWhisperSTT(
        transport,
        speech_detector=FixtureSpeechDetector(),
        audio_extractor=FixtureAudioExtractor(),
        chunk_seconds=40,
    )
    kwargs = {
        "guard": guard,
        "provider_consent_receipt": _openai_stt_consent(),
        "budget_reservation_receipt": _openai_stt_budget(),
        "secret_resolver": lambda ref: "secret",
        "checkpoint_dir": tmp_path / "checkpoints",
        "checkpoint_scope": "source-and-plan-digest",
    }

    with pytest.raises(RuntimeError, match="temporary_provider_failure"):
        provider.transcribe_bilingual(source, **kwargs)
    transcript = provider.transcribe_bilingual(source, **kwargs)

    assert len(transcript.korean) == 1
    assert [call[0] for call in transport.calls] == [
        "transcribe",
        "translate",
        "translate",
    ]
    assert [row["state"] for row in guard.budget_ledger] == [
        "reserved",
        "partially_consumed",
        "reserved",
        "consumed",
    ]
