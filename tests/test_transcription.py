from reelbrain.transcription import SubtitleFileSTT


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
