# ReelBrain Desktop Runtime Requirements

> Generated: 2026-07-21  
> Status: Approved for implementation

## Original Request

> "please implement all architecture and the left sidebar feature and pass the QA"

## Clarified Specification

ReelBrain Desktop must become a truthful creator-facing client for the governed
ReelBrain architecture rather than a static design shell.

### Runtime

- Use Codex-managed authentication; never read or copy Codex credentials.
- Keep the creator chat in one resumable Codex root thread.
- Produce one governed ReelBrain fan-out plan and execute four independent Codex
  persona threads: Meaning Scout, Hook Scout, Creator Advocate, and Context
  Guardian.
- Give every persona a unique, bounded capability packet. Persist only redacted
  grant metadata and token hashes.
- Validate persona results against canonical candidate IDs before accepting an
  editorial plan.
- Bound Codex and local-service calls with timeouts and terminate stalled child
  processes.
- Allow creator steering and cancellation to advance the fan-out epoch and make
  previous work stale.
- Keep render and publishing effects behind explicit approval. The desktop may
  inspect existing `CREATOR_REVIEW` artifacts but must not describe them as new
  renders from a later fan-out.

### Memory

- Treat memory as a behavioral prior, never evidence.
- Persist creator-approved preferences, feedback examples, proposals, versions,
  disabled state, and deletion tombstones locally.
- Support inspect, remember, confirm, edit, disable/re-enable, and forget.
- Require an explicit creator statement for durable or destructive changes.
- Prevent deleted preferences from returning after restart or stale import.

### Desktop information architecture

The left sidebar must open real surfaces rather than scroll to approximate
sections:

- Home: source drop, recent project, runtime health, and the next governed action.
- Projects: source/player, chat, real agent activity, candidate review, and current
  constraints.
- Your Taste: durable preference and proposal management.
- Review: inspect creator-review outputs and record approve/reject/revise feedback
  without claiming publication.
- Evidence: human-readable governance and denial timeline plus raw local artifacts.
- Settings: Codex account, local tools, privacy boundaries, and runtime limits.

### Truthfulness

- Never fabricate draft counts, agent progress, saved taste, approvals, or
  rendering state.
- Hide or disable controls that do not have behavior.
- State precisely that source bytes remain local until an approved provider or
  render effect; creator prompts still use Codex.

## Success Criteria

- A supported local source can launch four real Codex persona threads from one
  governed fan-out response and show live lane state.
- Unsupported/unprepared sources stop with a truthful prerequisite state.
- All left-sidebar destinations are functional and distinct.
- Memory mutations survive desktop restart and remain inspectable/correctable.
- Evidence survives restart and validates its hash chain.
- Production builds, native tests, Python tests, live Codex smoke tests, and the
  final QA gate pass.
