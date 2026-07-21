# ReelBrain Desktop

ReelBrain Desktop is a Tauri 2 + React creator shell around the local ReelBrain runtime and Codex app-server.

## What works

- Detects the current Codex account through `account/read` on the documented local app-server protocol.
- Launches the official `codex login` browser flow without reading or copying Codex credentials.
- Starts and resumes ReelBrain chat threads through `thread/start`, `thread/resume`, and `turn/start`.
- Produces one governed ReelBrain fan-out plan and runs Meaning Scout, Hook Scout,
  Creator Advocate, and Context Guardian as four independent read-only Codex
  persona threads.
- Streams real persona lifecycle events into the desktop lanes and validates
  returned candidate IDs before accepting an editorial plan.
- Accepts native drag-and-drop or file-dialog video selection.
- Runs local FFprobe and SHA-256 preflight before any provider effect.
- Discovers the latest local ReelBrain dogfood manifest and previews `CREATOR_REVIEW` drafts.
- Implements distinct Home, Projects, Your Taste, Review, Evidence, and Settings
  surfaces in the left sidebar.
- Persists creator-approved taste, feedback proposals, versions, disabled state,
  and deletion tombstones through ReelBrain's consent-first memory contract.
- Records hash-linked fan-out evidence, redacted capability grants, context
  authorizations, accepted submissions, steering revocations, and creator-review
  actions.
- Ships an optional official MCP stdio adapter (`reelbrain-mcp`) exposing the same
  governed plan, context, submission, steering, memory, review, and evidence
  services to Codex or another compatible host.
- Applies 90-second root-chat, 120-second persona, and 20-second local-service
  timeouts; stalled children are terminated.

The root chat and persona threads are intentionally read-only and approval-free.
Provider spend, rendering, and publishing remain separate governed effects. The
desktop can accept an editorial plan for render approval, but it does not claim
that existing local drafts were rendered by a newer fan-out.

## Run

```bash
cd desktop
npm install
npm run tauri dev
```

Build a macOS application bundle:

```bash
npm run tauri build -- --debug
open src-tauri/target/debug/bundle/macos/ReelBrain.app
```

Override local integration paths when needed:

```bash
REELBRAIN_CODEX_BIN=/absolute/path/to/codex \
REELBRAIN_PROJECT_ROOT=/absolute/path/to/ReelBrain \
npm run tauri dev
```

## Verify

```bash
npm run build
cd src-tauri
cargo check
cargo test
cd ../..
uv run pytest -q tests/test_desktop_runtime.py
uv run --extra mcp pytest -q tests/test_mcp_server.py
```

## Security boundary

- Codex owns and refreshes ChatGPT OAuth credentials.
- ReelBrain Desktop never reads `~/.codex/auth.json`.
- Local file access begins with native creator selection or drag-and-drop.
- Preflight does not upload the video or authorize provider spend.
- Taste changes use ReelBrain's local consent-first memory service with revision
  checks, provenance, and deletion fences; they are not inferred from ordinary
  chat without an explicit remember or confirmation action.
- Capability bearer tokens exist only in the active host call. Persisted runtime
  state contains hashes; the human-readable grant artifact contains neither the
  token nor its hash.
- A source without a canonical transcript/candidate catalog stops at
  `TRANSCRIPT_REQUIRED`; ReelBrain does not fabricate candidates or silently
  authorize transcription.
