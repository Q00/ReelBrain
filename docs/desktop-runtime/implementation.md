# ReelBrain Desktop Runtime Implementation

> Completed: 2026-07-21  
> Status: Implemented and under final QA

## Summary

The desktop product is now connected to ReelBrain's durable governance and
memory layer. It uses Codex app-server for one root chat thread and four
independent persona threads while ReelBrain validates canonical candidate IDs,
stores redacted grants, and writes hash-linked evidence.

## Files created

| File | Purpose |
|---|---|
| `reelbrain/desktop_state.py` | Restart-safe memory and creator-review state |
| `reelbrain/fanout.py` | Governed fan-out plans, capabilities, validation, epochs, evidence |
| `reelbrain/desktop_bridge.py` | Closed JSON-over-stdio local service surface |
| `reelbrain/mcp_server.py` | Official MCP stdio adapter with bounded tool descriptions |
| `tests/test_desktop_runtime.py` | Memory, fan-out, denial, steering, and persistence coverage |
| `docs/desktop-runtime/requirements.md` | Product/runtime requirements |
| `docs/desktop-runtime/architecture.md` | Ownership and data-flow design |

## Files modified

| File | Change |
|---|---|
| `reelbrain/memory.py` | Complete creator-scoped persistence and deletion-fence restoration |
| `desktop/src-tauri/src/lib.rs` | Bounded Codex app-server client, Python bridge, parallel fan-out, Tauri commands/events |
| `desktop/src/App.tsx` | Six real sidebar surfaces and governed creator interactions |
| `desktop/src/styles.css` | Full navigation, taste, review, evidence, settings, and live-state design |
| `desktop/src/services/reelbrain.ts` | Typed Tauri service mapping |
| `desktop/src/types.ts` | Runtime, evidence, memory, and fan-out types |
| `desktop/src-tauri/tauri.conf.json` | macOS `.app` bundling enabled |

## Runtime behavior

### Fan-out

1. Native selection produces local FFprobe and SHA-256 evidence.
2. ReelBrain resolves the digest against a canonical transcript/editorial catalog.
3. Four unique grants and tasks are created; only token hashes are persisted.
4. The Tauri host obtains authorized context and starts four Codex threads in
   parallel.
5. Each lane receives actual authorizing/running/completed/failed events.
6. ReelBrain rejects unknown IDs, stale epochs, and stale catalog/memory digests.
7. Accepted output stops at `READY_FOR_RENDER_APPROVAL`; rendering remains a
   separate governed effect.

The same operations are available to Codex through `reelbrain-mcp`. The desktop
uses the lower-overhead local bridge for its own process boundary; both adapters
delegate to the same ReelBrain services and trust rules.

### Memory

- Previously explicit founder preferences are migrated once into a durable local
  ledger with provenance.
- Episode feedback remains non-durable taste until two consistent examples create
  a proposal and the creator confirms it.
- Explicit remember, edit, disable, enable, and delete require a creator statement.
- Deletion removes content-bearing values and persists tombstones/fences.

### Navigation

- Home shows source ingestion, recent project, health, and architecture.
- Projects provides source/player, chat, live agents, real captions, drafts, and
  steering.
- Your Taste exposes full durable controls and proposals.
- Review records approve/reject/revise decisions while remaining
  `CREATOR_REVIEW` and `publish_ready=false`.
- Evidence combines fan-out and review events.
- Settings discloses Codex identity, local dependencies, timeouts, and data egress.

## Computer Use evidence

The packaged macOS app was operated through accessibility rather than judged only
from source code:

- all six sidebar destinations opened distinct functional surfaces;
- a taste preference was disabled and re-enabled, advancing memory revisions 1 →
  2 → 3;
- a creator revision request appeared in Evidence and remained
  `CREATOR_REVIEW`;
- a 371 MB creator source completed local FFprobe/SHA-256 preflight;
- four persona lanes simultaneously entered `running` and independently completed;
- ReelBrain accepted the grounded submission with plan digest
  `sha256:1c3ffbb84d3d559ebd95cd60d1120ea899a5e427e7ef7832b9c7e0d569bff716`;
- creator steering advanced the fan-out to `REQUIRES_REPLAN` and marked all prior
  lanes stale;
- the evidence projection reported six initial hash-linked events and validated
  successfully before the steering event.

## Known boundary

The desktop currently authorizes and validates an editorial plan for a later
render effect. It does not relabel the existing dogfood videos as outputs from
that new plan. Actual new rendering continues through ReelBrain's existing
RuntimeGuard/dogfood renderer after an explicit render approval.
