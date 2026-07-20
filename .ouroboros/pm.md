# ReelBrain

*Created At: 2026-07-20T07:29:35.575483+00:00*

## Goal

Create a personalized AI video-editing agent team that transforms raw talking-head footage from solo educational creators into publish-ready 30-60 second vertical short-form videos, while learning each creator's editing taste from explicit feedback over time.

## User Stories

1. **As a** Solo educational creator, **I want to** upload 20-minute talking-head footage and receive a captioned, tightly paced 30-60 second TikTok/Reel with highlights, reframing, music, and export, **so that** they can publish short-form educational content with minimal creator intervention instead of relying on a human editor.
2. **As a** Solo educational creator, **I want to** give explicit feedback on generated edits, **so that** ReelBrain remembers their editing taste and improves future edits based on their preference history.

## Constraints

- MVP targets solo educational creators.
- Input footage is 20-minute talking-head educational footage.
- Output must be a 30-60 second vertical short-form video suitable for TikTok/Reels.
- Selected highlights must be self-contained and understandable without the original video.
- Selected highlights must be educationally valuable.
- Selected highlights must not cut off mid-thought.
- Captions must achieve at least 95% word accuracy.
- Captions must have correct timing.
- Captions must use a readable maximum of two lines.
- Captions must use consistent creator-selected styling.
- Preference learning must be based on explicit creator feedback.

## Success Criteria

1. Generated videos meet the primary MVP acceptance criteria for highlight selection quality and caption accuracy/style.
2. The final video is publish-ready with minimal creator intervention.
3. The product can replace the editor for the defined MVP workflow rather than merely suggesting edits.
4. Future edits improve based on each creator's explicit feedback and preference history.

## Assumptions

- Any educational domain is acceptable for the MVP target persona.
- The MVP acceptance standard prioritizes highlight selection and caption accuracy/style over other video-editing dimensions.
- Creator-selected caption styling exists before or during generation, but the exact styling controls are not specified.

## Decide Later

The following items were deferred or identified as premature at this stage. They should be revisited when more context is available:

- For the MVP, what must be true for a generated 30-60 second video to be considered “acceptable” by the creator: highlight selection quality, caption accuracy/style, pacing, reframing, music choice, export format, or something else?
- Pacing quality is secondary for MVP acceptance.
- Reframing quality is secondary for MVP acceptance.
- Music choice is secondary for MVP acceptance.
- Export polish is secondary for MVP acceptance.

---
*PM ID: pm_seed_interview_20260720_072310*
*Interview ID: interview_20260720_072310*
