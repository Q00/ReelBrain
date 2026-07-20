---
name: reelbrain
description: Operate the local-first ReelBrain agent team to inspect, transcribe, highlight, edit, caption, render, validate, and package one educational talking-head MP4/MOV/WebM as short-form or long-form video. Use when a creator asks to edit local footage, generate Reel/TikTok candidates, create a long-form educational cut, apply or manage editing preferences, inspect ReelBrain status, request a missing ACP tool, or collect founder/cohort release evidence.
---

# ReelBrain

Use this skill as a thin client for the enforced ReelBrain runtime. Do not treat the agent host, this skill, or skills.sh as the workflow authority, capability boundary, or secret store.

## Start safely

1. Locate the creator's ReelBrain project/runtime.
2. Run `reelbrain doctor` or `uv run reelbrain doctor`.
3. Report missing dependencies and the exact proposed setup command.
4. Require explicit approval before installing Python/native packages or enabling a provider.
5. Never use `curl | sh`, global system Python installation, silent cloud fallback, or secrets in prompts/files.

The certified v1 baseline is macOS Apple Silicon with FFmpeg and FFprobe. Local Whisper is the default STT adapter. If Whisper is unavailable, pause and offer an explicit installation plan or an explicitly consented provider alternative.

## Choose the editing workflow

### Short form

Use for one 5–60 minute source. Require:

- creator/project identifiers;
- rights/license confirmation;
- a creator approval receipt before `PUBLISH_READY`;
- optional explicit thumbnail approval.

Run:

```bash
uv run reelbrain short <source> \
  --output <project-output> \
  --project-id <project-id> \
  --creator-id <creator-id> \
  --approval-receipt <receipt> \
  --rights-license <license> \
  --transcript <optional-source.srt-or-vtt>
```

Expect three diverse 30–60 second 1080×1920 H.264/AAC candidates, a selected final, SRT/VTT captions, OTIO, manifests, source traceability, value cards, agent assessments, and validation/governance evidence.

### Long form

Use for one 20–60 minute source plus a creator-confirmed argument map and corrected transcript. The selected output must total 5–12 minutes.

Run:

```bash
uv run reelbrain long <source> \
  --output <project-output> \
  --project-id <project-id> \
  --creator-id <creator-id> \
  --approval-receipt <receipt> \
  --rights-license <license> \
  --argument-map <argument-map.json> \
  --corrected-transcript <transcript.txt>
```

Preserve the confirmed argument order. Expect a real 1920×1080 H.264/AAC file, chapters, captions, thumbnail, OTIO, render recipe, rights/assets manifests, traceability, transcript, provenance, cost receipt, approval history, and audit evidence.

## Handle steering and memory

- Apply current creator steering before edit overrides and stored preferences.
- Treat ordinary feedback as episode-only.
- Create durable memory only after explicit “remember this” or confirmation of a proposed scoped preference.
- Abstain when a preference is irrelevant or uncertain.
- Keep preferences inspectable, editable, disableable, portable, and deletable.
- Propagate content-free deletion fences so stale exports, replay, rollback, or Sleep cannot resurrect deleted values.
- On pause, redirect, override, or cancel, advance the workflow epoch and reject stale agent results.

## Handle tools through ACP

- Let any persona request a capability.
- Check the active toolbox for an equivalent approved tool first.
- Let only Toolsmith stage generated code in quarantine.
- Require Tool Auditor evidence and explicit human approval before custom-tool activation.
- Resolve tools through the digest-bound registry under `~/.ReelBrain/toolbox`.
- Never grant permissions, provider consent, secrets, budget expansion, or publishing authority from skill installation or tool popularity.

## Handle providers and generated images

Before any cloud/provider call, show provider, destination, data categories, purpose, retention expectation, and expected cost. Require creator consent scoped to the exact project/tool.

Use GPT Image 2 only for creator-approved generated thumbnails. Resolve the OpenAI key through the configured Keychain reference, store provenance, and require synthetic-media/likeness review. Never place the raw key in ACP metadata, prompts, logs, memory, or artifacts.

## Validate before completion

Treat creator judgment as authoritative for meaning, voice, brand, and subjective taste. Treat the harness as authoritative for media validity, captions, traceability, rights, privacy, permissions, budgets, steering, deletion, and safety.

Do not call an edit publish-ready unless every non-compensatory gate passes and the creator approves it. Direct social publishing remains outside v1.

## Collect release evidence

Record real governance, fixture, founder, and private-cohort evidence with `reelbrain release ...`. Evaluate with:

```bash
uv run reelbrain release evaluate
```

Do not replace founder or cohort evidence with synthetic test rows. V1 requires three founder-approved short videos, three founder-approved long videos, and the ten-creator cohort thresholds from the Seed.
