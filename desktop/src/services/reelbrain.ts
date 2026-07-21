import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";
import type {
  EvidenceState,
  FanoutResult,
  MemoryState,
  ReviewRun,
  ReviewOutput,
  RevisionRenderPlan,
  RuntimeHealth,
  VideoPreflight,
} from "../types";

export async function preflightVideo(path: string): Promise<VideoPreflight> {
  return invoke<VideoPreflight>("preflight_video", { path });
}

export async function discoverReviewRun(): Promise<ReviewRun> {
  return invoke<ReviewRun>("discover_review_run");
}

export async function prepareMediaPreview(path: string): Promise<string> {
  return invoke<string>("prepare_media_preview", { path });
}

export async function persistChatImage(input: {
  name: string;
  mimeType: string;
  bytes: number[];
}): Promise<string> {
  return invoke<string>("persist_chat_image", { request: input });
}

export async function prepareChatImage(path: string): Promise<string> {
  return invoke<string>("prepare_chat_image", { path });
}

export async function startEditorialFanout(input: {
  sourcePath: string;
  sourceSha256: string;
  projectId?: string;
  creatorId?: string;
  currentSteering?: string | null;
}): Promise<FanoutResult> {
  const raw = await invoke<Record<string, unknown>>("start_editorial_fanout", { request: input });
  return {
    status: String(raw.status || "UNKNOWN"),
    fanoutId: (raw.fanoutId ?? raw.fanout_id) as string | undefined,
    epoch: raw.epoch as number | undefined,
    evidenceRevision: (raw.evidenceRevision ?? raw.evidence_revision) as number | undefined,
    planDigest: (raw.planDigest ?? raw.plan_digest) as string | undefined,
    planPath: (raw.planPath ?? raw.plan_path) as string | undefined,
    selectedCandidateIds: (raw.selectedCandidateIds ?? raw.selected_candidate_ids) as string[] | undefined,
    creatorReviewRequired: (raw.creatorReviewRequired ?? raw.creator_review_required) as boolean | undefined,
    publishReady: (raw.publishReady ?? raw.publish_ready) as boolean | undefined,
    rootAuthorityToken: (raw.rootAuthorityToken ?? raw.root_authority_token) as string | undefined,
    agentThreadIds: (raw.agentThreadIds ?? raw.agent_thread_ids) as string[] | undefined,
    agentResults: (raw.agentResults ?? raw.agent_results) as unknown[] | undefined,
    requiresCreatorApproval: (raw.requiresCreatorApproval ?? raw.requires_creator_approval) as boolean | undefined,
    requiredEffect: (raw.requiredEffect ?? raw.required_effect) as string | undefined,
    message: raw.message as string | undefined,
  };
}

export async function inspectCreatorMemory(): Promise<MemoryState> {
  return mapMemoryState(await invoke<Record<string, unknown>>("inspect_creator_memory"));
}

export async function mutateCreatorMemory(input: Record<string, unknown>): Promise<MemoryState> {
  return mapMemoryState(await invoke<Record<string, unknown>>("mutate_creator_memory", { request: input }));
}

export async function inspectFanoutEvidence(): Promise<EvidenceState> {
  const raw = await invoke<Record<string, unknown>>("inspect_fanout_evidence");
  return {
    fanouts: (raw.fanouts as Array<Record<string, unknown>> | undefined) ?? [],
    events: ((raw.events as Array<Record<string, unknown>> | undefined) ?? []).map((event) => ({
      sequence: event.sequence as number | undefined,
      eventId: event.event_id as string | undefined,
      eventType: String(event.event_type || "event"),
      fanoutId: event.fanout_id as string | undefined,
      actor: event.actor as string | undefined,
      decision: event.decision as "allow" | "deny" | undefined,
      reasonCode: event.reason_code as string | undefined,
      receiptId: event.receipt_id as string | undefined,
      createdAt: event.created_at as string | undefined,
      details: event.details as Record<string, unknown> | undefined,
    })),
    reviewEvents: ((raw.review_events as Array<Record<string, unknown>> | undefined) ?? []).map((event) => ({
      eventId: String(event.event_id),
      eventType: String(event.event_type),
      outputId: String(event.output_id),
      action: event.action as "approve" | "reject" | "revise",
      creatorStatement: String(event.creator_statement),
      resultingState: "CREATOR_REVIEW",
      publishReady: false,
      at: String(event.at),
    })),
  };
}

export async function steerEditorialFanout(input: Record<string, unknown>): Promise<Record<string, unknown>> {
  return invoke<Record<string, unknown>>("steer_editorial_fanout", { request: input });
}

export async function recordReviewAction(input: Record<string, unknown>): Promise<Record<string, unknown>> {
  return invoke<Record<string, unknown>>("record_review_action", { request: input });
}

export async function executeRevision(input: {
  baseOutputId: string;
  instruction: string;
  summary: string;
  jobId: string;
  renderPlan: RevisionRenderPlan;
}): Promise<ReviewOutput> {
  return invoke<ReviewOutput>("execute_revision", { request: input });
}

export async function planRevision(input: {
  instruction: string;
  mode: string;
  durationSeconds: number;
  title: string;
  rationale: string;
  requestId: string;
}): Promise<RevisionRenderPlan> {
  return invoke<RevisionRenderPlan>("plan_revision", { request: input });
}

export async function recordRevisionFeedback(input: {
  outputId: string;
  decision: "like" | "dislike" | "skip";
  reason?: string | null;
  creatorStatement: string;
}): Promise<{ output: ReviewOutput; eventId: string }> {
  return invoke<{ output: ReviewOutput; eventId: string }>("record_revision_feedback", { request: input });
}

export async function readRuntimeHealth(): Promise<RuntimeHealth> {
  return invoke<RuntimeHealth>("runtime_health");
}

function mapScope(raw: Record<string, unknown> | undefined) {
  return {
    outputMode: raw?.output_mode as string | null | undefined,
    contentKind: raw?.content_kind as string | null | undefined,
    language: raw?.language as string | null | undefined,
  };
}

function mapMemoryState(raw: Record<string, unknown>): MemoryState {
  return {
    creatorId: String(raw.creator_id),
    revision: Number(raw.revision),
    preferences: ((raw.preferences as Array<Record<string, unknown>> | undefined) ?? []).map((item) => ({
      id: String(item.id),
      category: String(item.category),
      value: String(item.value),
      scope: mapScope(item.scope as Record<string, unknown> | undefined),
      status: item.status as "active" | "disabled",
      explicit: Boolean(item.explicit),
      confidence: Number(item.confidence),
      version: Number(item.version),
      provenanceEventIds: (item.provenance_event_ids as string[] | undefined) ?? [],
      createdAt: String(item.created_at),
      updatedAt: String(item.updated_at),
    })),
    proposals: ((raw.proposals as Array<Record<string, unknown>> | undefined) ?? []).map((item) => ({
      proposalId: String(item.proposal_id),
      category: String(item.category),
      value: String(item.value),
      scope: mapScope(item.scope as Record<string, unknown> | undefined),
      confidence: Number(item.confidence),
      evidenceEventIds: (item.evidence_event_ids as string[] | undefined) ?? [],
    })),
    tombstones: ((raw.tombstones as Array<Record<string, unknown>> | undefined) ?? []).map((item) => ({
      preferenceId: String(item.preference_id),
      creatorId: String(item.creator_id),
      deletedAt: String(item.deleted_at),
      deletionReceiptId: String(item.deletion_receipt_id),
    })),
    principle: String(raw.principle),
  };
}

export async function selectVideo(): Promise<string | null> {
  const selected = await open({
    multiple: false,
    directory: false,
    filters: [
      {
        name: "Video",
        extensions: ["mp4", "mov", "m4v", "mkv", "webm"],
      },
    ],
  });
  return typeof selected === "string" ? selected : null;
}

export async function revealLocalEvidence(path?: string | null): Promise<void> {
  if (path) await openPath(path);
}
