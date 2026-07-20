# ReelBrain

ReelBrain is a local-first AI video-editing agent team for solo educational creators. It turns one pre-synchronized talking-head video into verified short-form and long-form local export packages, learns only creator-confirmed preferences, governs every runtime effect through ACP-backed capability checks, and improves bounded agent configurations through evidence-gated Sleep.

## Current platform

- Certified development baseline: macOS Apple Silicon
- Python: 3.11+
- Media runtime: FFmpeg and FFprobe
- Default local STT adapter: Whisper CLI, with no silent cloud fallback
- Delivery: local H.264/AAC files and evidence bundles; direct social publishing is intentionally excluded

## Setup

```bash
uv sync --dev
uv run reelbrain doctor
uv run reelbrain setup
uv run pytest -q
```

`doctor` reports required dependencies and platform compatibility. `setup` prints the complete local setup plan and waits for explicit approval. After reviewing it, run `uv run reelbrain setup --approve` to bootstrap the immutable toolbox and execute local FFmpeg/FFprobe conformance checks. Neither command installs packages, requests secrets, enables providers, or executes remote setup scripts. Missing dependencies are reported with proposed commands for the creator to approve separately.

## skills.sh client

The distributable thin-client skill is in [`skills/reelbrain`](skills/reelbrain). It can be published through a skills.sh-compatible registry, but installation grants no runtime permissions and does not install native or Python dependencies. The skill delegates editing, ACP governance, provider consent, memory, and release gates to the local ReelBrain runtime.

## Short-form dogfood

The default command transcribes locally with Whisper, fans candidates out to Meaning Scout, Hook Scout, Creator Advocate, and Context Guardian, then lets the Showrunner select three diverse 30–60 second candidates.

```bash
uv run reelbrain short ./source.mp4 \
  --output ./.reelbrain/projects/demo-short \
  --project-id demo-short \
  --creator-id founder \
  --approval-receipt founder-approved-demo-short \
  --rights-license creator-owned \
  --preferred-term Ouroboros \
  --preferred-term AgentOS \
  --thumbnail
```

The source must be a 5–60 minute MP4, MOV, or WebM with decodable video and audio. The command fails closed when Whisper is unavailable, rights are not approved, the source is unsupported, or three source-faithful candidates cannot be found.

If a creator already has an SRT or VTT transcript, pass `--transcript ./source.srt`. ReelBrain validates and uses it locally as the caption/highlight reference, so Whisper is not required and no provider call occurs.

## Long-form dogfood

Long-form accepts a creator-confirmed argument-map JSON array using the `TranscriptSegment` fields and a corrected transcript file.

```bash
uv run reelbrain long ./source.mp4 \
  --output ./.reelbrain/projects/demo-long \
  --project-id demo-long \
  --creator-id founder \
  --approval-receipt founder-approved-demo-long \
  --rights-license creator-owned \
  --argument-map ./argument-map.json \
  --corrected-transcript ./corrected-transcript.txt
```

The source must be 20–60 minutes, and the confirmed selection must total 5–12 minutes. ReelBrain preserves the creator-confirmed argument order.

## Release evidence

Release evidence is append-only and stored locally under `.reelbrain/release-evidence/` by default.

```bash
uv run reelbrain release record-governance --receipt governance-clean-1
uv run reelbrain release verify-fixtures
uv run reelbrain release record-founder \
  --run-id founder-short-1 \
  --output-mode short \
  --state PUBLISH_READY \
  --objective-gates-passed
uv run reelbrain release record-cohort \
  --creator-id creator-1 \
  --approves \
  --willing-to-publish \
  --minor-revisions 1 \
  --objective-gates-passed
uv run reelbrain release evaluate
```

The evaluator cannot be satisfied by unit tests alone. V1 requires real founder dogfood evidence for at least three short and three long videos, plus a ten-creator private cohort meeting the Seed thresholds.

## Safety and memory boundaries

- Ordinary feedback is episode-only. Durable memory requires explicit remember/confirmation.
- Current steering overrides edit overrides, which override confirmed scoped preferences.
- Preferences are inspectable, editable, disableable, portable, and deletable.
- Content-free deletion fences prevent stale exports from resurrecting deleted preferences.
- Runtime filesystem/tool effects pass through a deny-by-default reference monitor.
- Sleep can change only bounded configuration families and cannot mutate creator memory, tool code, permissions, secrets, consent, retention, budgets, or public skills packages.

## Specification

The complete product Seed is in [`.ouroboros/seeds/reelbrain-v1.yaml`](.ouroboros/seeds/reelbrain-v1.yaml).
