# ReelBrain Desktop Runtime Architecture

> Generated: 2026-07-21  
> Approach: governed local service with host-driven Codex fan-out

## Overview

The Tauri shell owns local process lifecycle and ephemeral Codex orchestration.
Python ReelBrain owns durable memory, candidate truth, capability grants,
accepted-plan validation, and hash-linked evidence.

```text
React UI
  | Tauri commands + progress events
  v
Rust desktop host
  |-- Codex app-server: root chat
  |-- Codex app-server x4: persona fan-out
  `-- Python JSON bridge
          |-- governed fan-out store
          |-- creator memory store
          `-- evidence/event projections
```

## Ownership

| Component | Responsibility | Location |
|---|---|---|
| React application | Navigation, creator interaction, ephemeral lane state | `desktop/src/` |
| Tauri host | Codex lifecycle, timeouts, parallel persona execution, native files | `desktop/src-tauri/` |
| Desktop bridge | Closed local command surface for durable ReelBrain operations | `reelbrain/desktop_bridge.py` |
| Fan-out service | Grants, snapshots, validation, epochs, evidence | `reelbrain/fanout.py` |
| Preference store | Consent-first behavioral prior and deletion fences | `reelbrain/memory.py` |

## Fan-out flow

1. The creator selects a local video and FFprobe/SHA-256 preflight succeeds.
2. ReelBrain resolves the source against a canonical transcript/editorial catalog.
3. If no catalog exists, the service returns `TRANSCRIPT_REQUIRED`; no agents run.
4. ReelBrain snapshots active taste and creates four unique capability grants.
5. The Rust host retrieves authorized context for each task and starts four
   independent read-only Codex threads concurrently.
6. Completion events update the UI lanes without persisting Codex scheduling
   state.
7. ReelBrain validates all four results and persists an accepted editorial-plan
   digest. Unknown candidates or stale snapshots fail closed.
8. Rendering remains a separately approved governed effect.

## Persistence

```text
.reelbrain/desktop/
├── memory/
│   └── creator-default.json
└── fanout/
    └── fanout_<id>/
        ├── evidence-record.json
        ├── evidence-events.jsonl
        ├── source-snapshot.json
        ├── candidate-catalog.json
        ├── memory-snapshot.json
        ├── capability-grants.redacted.json
        ├── submission.json
        └── editorial-plan.json
```

Writes use temporary files plus atomic replacement. Evidence events are
append-only and hash-linked. Capability bearer tokens are returned only to the
current host call; persisted records contain token hashes and redacted scopes.

## Security boundaries

- Codex credentials stay inside Codex-managed storage.
- Persona threads are read-only with `approvalPolicy=never`.
- The desktop bridge accepts JSON over local stdio and has no network listener.
- Durable memory changes require an explicit creator statement.
- Deleted values are removed from content-bearing records and retained only as
  content-free tombstones/fences.
- Child processes have bounded execution time and are killed after timeout.
