# Iterative Draft Feedback Loop Requirements

> Generated: 2026-07-22  
> Status: Implemented

## Original requirement

After a creator approves a revision proposal, ReelBrain must produce and show a real draft version on Projects. The creator then chooses Like or Dislike. Like becomes creator taste with evidence. Dislike asks for the reason, uses that correction to create another draft, and repeats. Once feedback is answered, that version leaves Review but remains in project history.

## Required behavior

- Approval is accepted only through the visible Yes control bound to a pending revision workflow.
- Approval starts a non-destructive local render; recording approval alone is not presented as a completed edit.
- Render progress remains visible through preparation, rendering, verification, completion, or failure.
- A draft is registered only when its video exists, FFprobe verifies audio/video streams, its duration is sane, a thumbnail exists, and its digest differs from the parent.
- The new version becomes selected on Projects and remains playable from version history.
- Every pending revision displays explicit Like, Dislike, and Skip choices.
- Like writes a single-use feedback event and an explicit creator preference linked to that event as provenance.
- Dislike requires answers for what is wrong and what the next draft must do; an optional answer identifies what must be preserved.
- Skip records an intentional no-direct-feedback decision, stores the original revision request as a tentative inferred taste episode with evidence, starts no render, and removes the version from Review while preserving history. The episode does not become active taste without sufficient consistent examples and creator confirmation.
- Submitting Dislike writes correction evidence, removes the answered version from Review, and automatically starts the next non-destructive version.
- Answered versions remain available in project/version history.
- Memory remains a behavioral prior and never becomes source evidence.
- No feedback action publishes a video.

## Acceptance criteria

1. A successful approval produces a changed `draft.mp4`, a thumbnail, an evidence record, and a version catalog entry.
2. The created version appears immediately on Projects as `vN`.
3. Like/Dislike/Skip are explicit buttons; typed conversational text cannot select an action.
4. Like persists external revision-feedback provenance on the resulting taste preference.
5. Dislike cannot be submitted without a reason and next-draft direction.
6. An answered version is absent from Review and present in Projects history after restart.
7. Duplicate feedback for the same version is rejected.
