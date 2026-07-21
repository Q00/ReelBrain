# ReelBrain Hackathon Submission — Writer Handoff

> Date: 2026-07-21  
> Audience: hackathon submission writer, video editor, and demo presenter  
> Narrative mode: written as if the planned desktop demo has been completed  
> Source architecture: `reelbrain-governed-evolving-fanout-harness-final-design-20260721.md`

## 1. What We Are Submitting

ReelBrain is a desktop AI video-editing application for educational creators.

A creator connects Codex, drags in a raw talking-head video, and chats with ReelBrain about what they want. ReelBrain then sends the footage through a team of specialized editorial agents that independently look for meaning, hooks, creator fit, and missing context. The agents return grounded candidate IDs rather than inventing cuts. ReelBrain validates the results, renders reviewable Shorts and a long-form draft locally, and remembers only the creator-approved taste that should carry into future projects.

The submission demonstrates more than agent fan-out. It demonstrates a governed agent system where every agent receives limited permissions, unauthorized actions are denied and recorded, stale work is rejected after the creator changes direction, and the final render is connected to an inspectable evidence trail.

### Submission package

- ReelBrain Desktop demo for macOS Apple Silicon
- official Codex-managed sign-in flow
- drag-and-drop local video ingestion
- conversational creator steering
- four visible editorial agent lanes
- grounded Short and long-form proposals
- local FFmpeg/Pillow rendering
- `Your Taste` preference management
- a live permission-denial demonstration
- a linked governance and evidence bundle
- architecture document and repository
- short product demo video

## 2. Recommended Positioning

### Product name

**ReelBrain**

### Primary tagline

> Your AI editing team, with your taste.

### One-sentence description

> ReelBrain turns raw creator videos into grounded, reviewable edits using a governed team of Codex agents that learns only the preferences the creator chooses to keep.

### Short description

> Connect Codex, drop in a video, and talk to ReelBrain. Four editorial agents search for meaning, hooks, creator fit, and context while ReelBrain controls their permissions, validates every cut, remembers your approved taste, and renders local review packages with a complete evidence trail.

### Core story for judges

Most agent demos focus on how many agents can run. ReelBrain focuses on what happens after agents begin acting.

- What was each agent allowed to access?
- Did it use real source evidence?
- What happens when it exceeds its authority?
- What happens when the creator changes direction?
- How does the system learn taste without silently inventing permanent memory?
- Why should the creator trust the final render?

ReelBrain answers those questions with grounded candidate catalogs, per-agent capability grants, denial receipts, workflow epochs, creator-confirmed memory, and accepted-plan render gates.

## 3. The Three Proof Moments

The submission and demo should emphasize these three moments.

### Proof 1 — Real multi-agent editorial work

Codex visibly runs four independent lanes:

1. Meaning Scout
2. Hook Scout
3. Creator Advocate
4. Context Guardian

Each agent receives the same source-grounded candidate catalog but evaluates it through a different editorial lens.

### Proof 2 — A denied action becomes evidence

One persona attempts an action outside its grant, such as render access or requesting a candidate outside its assigned context. ReelBrain blocks the action before any effect and creates a denial receipt visible in the desktop evidence timeline.

This is the strongest differentiation moment.

### Proof 3 — Creator taste transfers safely

After reviewing a Short, the creator says:

> Remember that I prefer technical tension over sensational hooks.

ReelBrain stores it as a scoped, inspectable preference. In the next relevant project, Creator Advocate receives that preference in its authorized taste snapshot. The creator can later edit, disable, or forget it.

## 4. Language Guidance for the Writer

### Prefer these phrases

- governed agent fan-out
- source-grounded editorial candidates
- per-agent permission envelope
- creator-approved taste
- inspectable evidence trail
- local review package
- stale work rejection
- Connect Codex
- behavior prior, not source evidence

### Translate technical ideas into judge-friendly language

| Internal term | Submission language |
|---|---|
| Capability Packet | a limited permission envelope for each agent |
| Denial receipt | proof that an unauthorized action was blocked |
| Workflow epoch | a way to reject work created before the creator changed direction |
| Memory snapshot digest | the exact version of creator taste used for this edit |
| Accepted-plan digest | proof that the rendered video matches a validated editorial plan |
| Evidence bundle | an inspectable audit trail connecting source, agents, memory, and output |

### Do not claim

- Do not say OpenAI usage is free. Editorial reasoning uses the creator's Codex access or quota.
- Do not say ReelBrain controls every arbitrary host or shell tool. It controls effects routed through ReelBrain.
- Do not say ReelBrain implements its own OpenAI OAuth. Codex owns the supported login flow.
- Do not say ReelBrain automatically remembers everything. Episode feedback remains temporary unless remembered or confirmed.
- Do not say outputs are automatically publish-ready. They stop at `CREATOR_REVIEW`.
- Do not say ReelBrain publishes directly to social platforms.
- Do not describe creator taste as factual evidence about the video.

## 5. Devpost-Style Submission Draft

## Inspiration

Video editing tools remember files and timelines, but they rarely remember the creator.

Every time an educational creator starts a new project, they have to repeat the same direction: keep the full caveat, do not use sensational hooks, preserve their terminology, keep the pacing natural, and make the captions bilingual. Generic AI editors can generate clips quickly, but their decisions are often opaque. Once multiple agents are involved, the creator also loses visibility into what each agent saw, what it was allowed to do, and why a final cut should be trusted.

We wanted to build an editing system that grows with the creator without taking control away from them.

That led us to one guiding principle:

> Memory is a behavioral prior, not evidence.

ReelBrain should remember how a creator likes to work, but every cut still has to come from the actual source. It should use multiple editorial perspectives, but every agent should have bounded authority. It should move quickly, but the creator should always be able to inspect, steer, forget, and review.

## What it does

ReelBrain is a desktop AI video-editing team for educational creators.

The creator connects Codex, drags a local video into the application, and tells ReelBrain what kind of edit they want. ReelBrain analyzes the media locally, prepares a timestamped candidate catalog, and asks four independent editorial agents to review it:

- **Meaning Scout** looks for self-contained explanations and useful ideas.
- **Hook Scout** finds compelling openings with a real payoff.
- **Creator Advocate** applies the creator's approved voice and taste.
- **Context Guardian** protects caveats, context, and complete thoughts.

The agents never control timestamps or invent source boundaries. They rank ReelBrain's grounded candidate IDs and explain their reasoning. ReelBrain validates the combined proposal before issuing permission to render.

From one raw video, ReelBrain can produce:

- three grounded 30–60 second vertical Shorts;
- one 10–15 minute long-form draft;
- Korean and English subtitle files;
- exact burned bilingual captions;
- generated thumbnail backgrounds with deterministic title overlays;
- a local `CREATOR_REVIEW` package;
- an evidence trail connecting the source, agents, permissions, taste memory, editorial plan, and rendered output.

The desktop application also includes **Your Taste**, where creators can inspect what ReelBrain has learned, see where a preference came from, confirm proposed preferences, edit their values or scope, disable them, and forget them completely.

## How we built it

We separated the system into two ownership layers.

**Codex owns ephemeral orchestration.** It creates the agent threads, runs the four editorial lanes, manages concurrency, and returns their results.

**ReelBrain owns durable trust.** It creates the source-grounded catalogs, issues per-agent permission envelopes, records allowed and denied actions, validates every result, stores creator-approved taste, and gates rendering on an accepted plan.

The desktop client is designed as a Tauri and React application. It connects to a local Codex app-server for the supported Codex authentication and conversation experience, and to the local ReelBrain Python runtime for media, governance, memory, and evidence. Codex credentials remain in Codex-managed storage rather than passing through ReelBrain.

ReelBrain exposes its workflow through MCP tools:

- plan the fan-out;
- provide scoped candidate context;
- submit the correlated persona results;
- steer or invalidate stale work;
- render only an accepted plan;
- record creator feedback and memory decisions.

Each agent receives a capability grant limited by task, tool, candidate IDs, memory categories, path scope, network destinations, budget, expiry, and workflow version. ReelBrain verifies the grant on every controlled tool call and records an allow or denial receipt.

For media processing, we use Python, FFmpeg, FFprobe, and Pillow. Rendering is local and deterministic. Shorts use a centered full source frame over a blurred vertical background, while long-form drafts preserve the selected segment order. Pillow rasterizes exact Korean and English overlays so the output does not depend on optional FFmpeg text libraries.

Creator taste builds on a consent-first preference store. Ordinary feedback stays local to the episode. Explicit “remember” requests become durable immediately, while inferred preferences require repeated evidence and creator confirmation. Every preference is scoped, versioned, inspectable, editable, disableable, portable, and deletable.

## Challenges we ran into

### Separating agent execution from agent trust

Our first instinct was to make the MCP layer track the entire fan-out workflow. That would have turned ReelBrain into a scheduler. The opposite extreme—a stateless transport that only forwards prompts—would have removed our most important differentiation.

We solved this by splitting state ownership. Codex owns what is running. ReelBrain owns what was allowed, attempted, accepted, remembered, and rendered.

### Making permissions real instead of decorative

It is easy to attach a JSON object to an agent and call it a permission model. We needed the permission envelope to be enforceable. ReelBrain therefore verifies every controlled request against a server-side grant and produces an inspectable denial receipt when the request exceeds its scope.

### Preventing stale agent work

Creators change their minds while editing. A technically valid agent result may still be wrong if it was generated before the latest direction. We introduced workflow epochs and snapshot digests so results from an older source, taste version, or creator steering event are rejected rather than silently accepted.

### Learning taste without memory poisoning

Automatically turning every click into permanent memory would make ReelBrain unpredictable. We designed a consent-first loop where one-off feedback remains temporary, repeated patterns become proposals, and durable inferred preferences require confirmation.

### Rendering exact multilingual captions

The available FFmpeg build did not reliably include every optional text-rendering library. We generated exact text overlays with Pillow and composited them through FFmpeg while also preserving SRT and ASS sidecars.

### Making a complex architecture feel simple

The backend includes agents, capabilities, evidence, epochs, memory, provider governance, and rendering. The desktop experience still had to feel like three actions: connect, drop a video, and talk to ReelBrain. Designing that translation was as important as the runtime itself.

## Accomplishments that we're proud of

- We created a real four-agent editorial workflow through Codex rather than simulating personas inside one prompt.
- Every editorial selection remains grounded in ReelBrain-owned candidate IDs and timestamps.
- We made an unauthorized agent action visible: it is blocked before effect and appears as a denial receipt in the evidence timeline.
- We connected creator steering to stale-work rejection, so old agent results cannot overwrite new direction.
- We built reversible creator taste memory with explicit provenance, scope, versioning, confirmation, disabling, and deletion fences.
- We kept rendering local and deterministic with real H.264/AAC outputs, bilingual subtitles, thumbnails, and review manifests.
- We moved editorial reasoning out of ReelBrain-managed Responses API calls and into the creator's Codex session while preserving separate governance for transcription and image generation.
- We connected the entire run—from source digest to final package—through one inspectable evidence bundle.
- We made the workflow accessible through a desktop experience instead of requiring creators to understand a CLI, MCP, or agent orchestration.

## What we learned

The number of agents is not the moat.

The important questions are whether those agents are grounded, whether their permissions are enforceable, whether their actions are observable, and whether their learning remains under creator control.

We also learned that memory has to be designed as a product surface, not a hidden model feature. When creators can inspect and correct what the system believes about their taste, personalization becomes trustworthy rather than mysterious.

Another key lesson was that orchestration state and governance state should not live in the same place. The host is best at running agents. ReelBrain is best at preserving ground truth, permission decisions, creator memory, and effect evidence.

Finally, local-first media processing changes the product relationship. The creator can use powerful hosted reasoning while keeping source files, timelines, renders, and memory artifacts under local control until they explicitly authorize an external effect.

## What's next for ReelBrain

Our next step is to turn the hackathon build into a creator-ready private beta.

We plan to:

- finish signed and notarized macOS packaging;
- add Windows support;
- support local transcription and creator-supplied thumbnails for a near-zero external API-cost workflow;
- expand the editing toolbox beyond talking-head educational videos;
- add deterministic scoring across multiple competing Showrunner proposals;
- build richer before-and-after taste evaluation;
- test the product with a private cohort of educational creators;
- add optional encrypted project synchronization without weakening the local-first default;
- introduce more agent roles only when they demonstrate measurable creator value;
- preserve the same non-negotiable boundary: agents may propose, but ReelBrain must prove and creators must approve.

## 6. Suggested Demo Video Structure

### 0:00–0:15 — The problem

Show a raw talking-head video and explain:

> AI editors can generate clips, but they forget your taste and hide how their agents make decisions.

### 0:15–0:30 — Connect and drop

- open ReelBrain Desktop;
- show `Codex connected`;
- drag in the raw video;
- show local preflight before any provider effect.

### 0:30–0:50 — Four agents

- type: “Keep this technical, not sensational”;
- show the four editorial lanes running;
- open one grounded candidate with timestamps and rationale.

### 0:50–1:05 — Governance kill shot

- show Context Guardian attempting an unauthorized action;
- show the denial immediately;
- open the human-readable receipt.

Suggested line:

> Codex runs the agents. ReelBrain decides what they are allowed to do and proves what happened.

### 1:05–1:25 — Review and render

- preview the three Shorts and long-form draft;
- show bilingual captions and thumbnail;
- show `CREATOR_REVIEW`, not auto-publish.

### 1:25–1:40 — Taste growth

- say: “Remember that I prefer technical tension over sensational hooks”;
- open `Your Taste`;
- show the preference scope and provenance;
- begin another project and show Creator Advocate receiving the new taste snapshot.

### 1:40–1:50 — Closing

> ReelBrain is an AI editing team that grows with the creator—without asking the creator to give up control.

## 7. Submission Asset Checklist

- [ ] ReelBrain logo and app icon
- [ ] one desktop workspace hero screenshot
- [ ] one `Your Taste` screenshot
- [ ] one evidence/denial screenshot
- [ ] 90–120 second demo video
- [ ] three rendered Short samples
- [ ] one long-form draft excerpt
- [ ] sample evidence bundle with secrets and raw tokens removed
- [ ] architecture diagram
- [ ] repository link
- [ ] installation or demo instructions
- [ ] privacy and data-flow summary
- [ ] explicit note that outputs stop at creator review

## 8. Final Writer Checklist

Before publishing the submission, verify every past-tense claim against the actual demo build.

- [ ] Codex sign-in is demonstrated through a supported flow.
- [ ] Four real agent threads are visible.
- [ ] The denial event is produced by the runtime, not mocked in the UI.
- [ ] At least one rendered media artifact is real and locally validated.
- [ ] The memory preference appears in a subsequent relevant run.
- [ ] Deleted preference content is absent from the memory view.
- [ ] Editorial API-cost claims distinguish Codex usage from OpenAI API billing.
- [ ] Provider-backed transcription and image generation are disclosed when used.
- [ ] No secret, credential, raw capability token, or private path appears in screenshots or evidence samples.
- [ ] All outputs are described as `CREATOR_REVIEW` unless every later release gate truly passes.
