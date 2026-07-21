export type ConnectionStatus = {
  connected: boolean;
  authMode: "chatgpt" | "apiKey" | "unknown" | null;
  email?: string | null;
  planType?: string | null;
  requiresOpenaiAuth: boolean;
  detail: string;
};

export type VideoPreflight = {
  path: string;
  name: string;
  sizeBytes: number;
  sha256: string;
  durationSeconds?: number | null;
  width?: number | null;
  height?: number | null;
  hasVideo: boolean;
  hasAudio: boolean;
  status: "ready" | "unsupported";
  message: string;
};

export type ReviewOutput = {
  outputId: string;
  mode: "short" | "long";
  title: string;
  durationSeconds: number;
  video: string;
  thumbnail: string;
  sourceRange: [number, number];
  rationale: string;
  status: string;
  captionAccuracyStatus: string;
  captionPreview: CaptionPreview[];
  baseOutputId?: string | null;
  parentOutputId?: string | null;
  version: number;
  isRevision: boolean;
  revisionSummary?: string | null;
  feedbackStatus: "pending" | "liked" | "disliked" | "skipped";
  feedbackReason?: string | null;
  feedbackEventId?: string | null;
  createdAt?: string | null;
};

export type CaptionPreview = {
  startSeconds: number;
  endSeconds: number;
  text: string;
};

export type ReviewRun = {
  available: boolean;
  status: string;
  projectTitle: string;
  manifestPath?: string | null;
  outputs: ReviewOutput[];
};

export type ChatMessage = {
  id: string;
  role: "creator" | "reelbrain" | "system" | "approval";
  sender?: string;
  text: string;
  time: string;
  activities?: ChatActivity[];
  attachments?: ChatAttachment[];
  approvalRequest?: ToolApprovalRequest | null;
  workflow?: WorkflowProgress | null;
  draftFeedback?: DraftFeedbackPrompt | null;
};

export type DraftFeedbackPrompt = {
  outputId: string;
  version: number;
};

export type RevisionProgress = {
  jobId: string;
  phase: string;
  progress: number;
  detail: string;
  status: "running" | "completed" | "failed";
  outputId?: string | null;
};

export type RevisionRenderPlan = {
  supported: boolean;
  contrast: number;
  saturation: number;
  sharpen: number;
  audioTargetLufs: number;
  rationale: string;
  unsupportedReason?: string | null;
};

export type WorkflowStatus = "awaiting_approval" | "running" | "completed" | "blocked" | "failed";

export type WorkflowProgress = {
  id: string;
  title: string;
  phase: string;
  status: WorkflowStatus;
  progress: number;
  detail: string;
  videoChanged: boolean;
  outputId?: string | null;
};

export type ChatAttachment = {
  id: string;
  path: string;
  name: string;
  previewUrl?: string;
};

export type ChatResult = {
  threadId: string;
  response: string;
  activities: ChatActivity[];
  approvalRequest?: ToolApprovalRequest | null;
  revisionProposal?: { summary: string } | null;
};

export type ToolApprovalStatus =
  | "pending_creator_approval"
  | "approved_for_quarantined_build"
  | "building_quarantined_tool"
  | "build_or_test_failed"
  | "denied_by_creator"
  | "quarantined_pending_deploy_approval"
  | "deployment_denied_by_creator"
  | "deployed";

export type ToolApprovalRequest = {
  approvalId: string;
  requestedBy: string;
  toolName: string;
  purpose: string;
  reasonMissing: string;
  capabilities: string[];
  dependencies: string[];
  permissions: string[];
  dataEffects: string[];
  status: ToolApprovalStatus;
  createdAtMs: number;
  updatedAtMs: number;
  approvalReceiptId?: string | null;
  creatorStatement?: string | null;
  buildPath?: string | null;
  artifactDigest?: string | null;
  testStatus?: "building" | "passed" | "failed" | null;
  testSummary?: string | null;
  auditorReport?: Record<string, unknown> | null;
  deployedToolPath?: string | null;
};

export type ChatActivityStatus = "running" | "completed" | "failed";

export type ChatActivity = {
  id: string;
  actor: string;
  kind: string;
  title: string;
  detail?: string | null;
  status: ChatActivityStatus;
};

export type ChatActivityEvent = {
  requestId: string;
  activity: ChatActivity;
};

export type AgentProfile = {
  id: string;
  name: string;
  role: string;
  systemPrompt: string;
};

export type AgentProfileState = {
  revision: number;
  profiles: AgentProfile[];
};

export type TeamChatResult = ChatResult & {
  agentThreadIds: string[];
  participants: string[];
};

export type TastePreference = {
  id: string;
  category: string;
  value: string;
  scope: PreferenceScope;
  status: "active" | "disabled";
  explicit: boolean;
  confidence: number;
  version: number;
  provenanceEventIds: string[];
  createdAt: string;
  updatedAt: string;
};

export type PreferenceScope = {
  outputMode?: string | null;
  contentKind?: string | null;
  language?: string | null;
};

export type TasteProposal = {
  proposalId: string;
  category: string;
  value: string;
  scope: PreferenceScope;
  confidence: number;
  evidenceEventIds: string[];
};

export type MemoryState = {
  creatorId: string;
  revision: number;
  preferences: TastePreference[];
  proposals: TasteProposal[];
  tombstones: Array<{
    preferenceId: string;
    creatorId: string;
    deletedAt: string;
    deletionReceiptId: string;
  }>;
  principle: string;
};

export type AgentLaneStatus = "ready" | "authorizing" | "running" | "completed" | "failed" | "stale";

export type AgentProgress = {
  fanoutId: string;
  persona: string;
  status: AgentLaneStatus;
  detail: string;
  threadId?: string | null;
};

export type FanoutResult = {
  status: "TRANSCRIPT_REQUIRED" | "READY_FOR_RENDER_APPROVAL" | string;
  fanoutId?: string;
  epoch?: number;
  evidenceRevision?: number;
  planDigest?: string;
  planPath?: string;
  selectedCandidateIds?: string[];
  creatorReviewRequired?: boolean;
  publishReady?: boolean;
  rootAuthorityToken?: string;
  agentThreadIds?: string[];
  agentResults?: unknown[];
  requiresCreatorApproval?: boolean;
  requiredEffect?: string;
  message?: string;
};

export type EvidenceEvent = {
  sequence?: number;
  eventId?: string;
  eventType: string;
  fanoutId?: string;
  actor?: string;
  decision?: "allow" | "deny";
  reasonCode?: string;
  receiptId?: string;
  createdAt?: string;
  details?: Record<string, unknown>;
};

export type ReviewEvent = {
  eventId: string;
  eventType: string;
  outputId: string;
  action: "approve" | "reject" | "revise";
  creatorStatement: string;
  resultingState: "CREATOR_REVIEW";
  publishReady: false;
  at: string;
};

export type EvidenceState = {
  fanouts: Array<Record<string, unknown>>;
  events: EvidenceEvent[];
  reviewEvents: ReviewEvent[];
};

export type RuntimeHealth = {
  codex: boolean;
  python: boolean;
  ffmpeg: boolean;
  ffprobe: boolean;
  workspace: string;
  chatTimeoutSeconds: number;
  agentTimeoutSeconds: number;
  bridgeTimeoutSeconds: number;
};
