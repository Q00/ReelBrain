# Iterative Draft Feedback Loop Architecture

> Generated: 2026-07-22  
> Approach: Governed, local, versioned revision jobs

## Flow

```text
Showrunner proposal
  -> creator Yes
  -> durable review event
  -> Style Editor LLM converts natural-language feedback to a bounded render plan
  -> isolated FFmpeg revision job
  -> stream/duration/digest/thumbnail verification
  -> version catalog + evidence.json
  -> Projects selects vN
  -> Like / Dislike / Skip
       Like -> feedback event -> explicit taste + provenance
       Dislike -> correction event -> explicit correction taste -> render vN+1
       Skip -> feedback event -> tentative request-derived taste episode -> no render
```

## Components

| Component | Responsibility | Location |
| --- | --- | --- |
| Revision executor | Renders a new local file, emits progress, validates output, never overwrites a parent | `desktop/src-tauri/src/lib.rs` |
| Revision planner | Uses the Style Editor LLM to choose bounded contrast, saturation, sharpening, and loudness parameters; rejects unsupported edit types rather than faking them | `desktop/src-tauri/src/lib.rs` |
| Revision catalog | Restart-safe version and feedback state | `.reelbrain/desktop/revision-drafts.json` |
| Per-version evidence | Approved instruction, lineage, renderer, source/output digests | `.reelbrain/desktop/revisions/*/evidence.json` |
| Feedback audit | Single-use creator Like/Dislike events | `.reelbrain/desktop/revision-feedback.jsonl` |
| Preference provenance adapter | Links creator feedback evidence to durable behavioral priors | `reelbrain/desktop_state.py` |
| Projects UI | Selected draft, version history, feedback card, iterative correction form | `desktop/src/App.tsx` |
| Review UI | Pending-only decision queue | `desktop/src/App.tsx` |

## State rules

- `feedback_status=pending` means the draft is present in Review.
- `feedback_status=liked|disliked` removes it from Review without deleting it.
- A revision record carries both `base_output_id` and `parent_output_id`, preserving complete lineage.
- Only a validated changed digest can create a new version.
- Original artifacts are never overwritten or deleted by the loop.

## Interaction decisions

- The common path is two visible choices: Like and Dislike.
- Dislike progressively reveals three concise questions instead of opening a dense modal.
- Render status stays attached to the chat turn that authorized it.
- Version history remains adjacent to the video it affects.
- Reduced-motion and increased-contrast behavior inherit the existing desktop accessibility rules.
