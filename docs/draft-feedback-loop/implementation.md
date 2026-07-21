# Iterative Draft Feedback Loop Implementation

> Generated: 2026-07-22

## Implemented

- Added `execute_revision` and `record_revision_feedback` Tauri commands.
- Added an isolated revision directory per job and version.
- Added continuous `revision-progress` events parsed from FFmpeg progress output.
- Added a model-judged render planner so approved natural-language requests and Dislike reasons select bounded render parameters without keyword classifiers.
- Added FFprobe, duration, regular-file, digest-difference, and thumbnail gates.
- Added restart-safe revision catalog loading into the existing review run.
- Added version lineage and feedback fields to desktop types.
- Added a Projects version strip and selected-version playback.
- Added Like/Dislike/Skip cards in Projects, Review, and the originating chat turn.
- Added targeted Dislike questions and automatic next-version rendering.
- Added Skip as a request-derived tentative taste episode; it remains inactive until repeated evidence and creator confirmation.
- Changed Review to show only pending feedback versions.
- Added feedback events to Memory & Evidence.
- Linked Like/Dislike evidence IDs into preference provenance.
- Added single-use feedback and external provenance tests.

## Verification

- `npm run build`
- `cargo test`
- `uv run pytest tests/test_desktop_runtime.py -q`

## Safety boundary

The current revision executor is a bounded local finishing pass behind the semantic revision workflow. It preserves duration and existing burned captions, validates changed media, and records the approved natural-language instruction as evidence. It does not publish, upload, overwrite earlier versions, or claim source facts from memory.
