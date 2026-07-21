import { convertFileSrc } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  Activity,
  ArrowRight,
  Bot,
  Captions,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleUserRound,
  Clock3,
  Edit3,
  Eye,
  FilePenLine,
  FileCheck2,
  Film,
  FolderKanban,
  Heart,
  Home,
  ImagePlus,
  Info,
  LoaderCircle,
  MessageSquareText,
  Maximize2,
  Minimize2,
  Pause,
  PauseCircle,
  PictureInPicture2,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Scissors,
  Send,
  Settings,
  ShieldCheck,
  SkipBack,
  SkipForward,
  Sparkles,
  Terminal,
  ThumbsDown,
  ThumbsUp,
  ToggleLeft,
  ToggleRight,
  Trash2,
  UploadCloud,
  Volume2,
  VolumeX,
  WandSparkles,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BrainMark } from "./components/BrainMark";
import {
  chatWithAgent,
  chatWithReelBrain,
  chatWithTeam,
  buildAndTestTool,
  connectCodex,
  decideToolApproval,
  deployTestedTool,
  readAgentProfiles,
  readCodexStatus,
  updateAgentProfile,
} from "./services/codex";
import {
  discoverReviewRun,
  executeRevision,
  inspectCreatorMemory,
  inspectFanoutEvidence,
  mutateCreatorMemory,
  persistChatImage,
  planRevision,
  preflightVideo,
  prepareChatImage,
  prepareMediaPreview,
  readRuntimeHealth,
  recordReviewAction,
  recordRevisionFeedback,
  revealLocalEvidence,
  selectVideo,
  startEditorialFanout,
  steerEditorialFanout,
} from "./services/reelbrain";
import type {
  AgentLaneStatus,
  AgentProfile,
  AgentProfileState,
  AgentProgress,
  ChatActivity,
  ChatActivityEvent,
  ChatAttachment,
  ChatMessage,
  ConnectionStatus,
  EvidenceState,
  FanoutResult,
  MemoryState,
  PreferenceScope,
  ReviewOutput,
  ReviewRun,
  RevisionProgress,
  RuntimeHealth,
  TastePreference,
  ToolApprovalRequest,
  VideoPreflight,
  WorkflowProgress,
} from "./types";

type ViewName = "Home" | "Projects" | "Memory" | "Review" | "Settings";

type PendingRevision = {
  workflowId: string;
  creatorRequest: string;
  proposal: string;
  outputId?: string | null;
};

const DEFAULT_STATUS: ConnectionStatus = {
  connected: false,
  authMode: null,
  requiresOpenaiAuth: true,
  detail: "Checking Codex…",
};

const AGENTS = [
  { id: "meaning-scout", name: "Story Editor", detail: "Builds a complete educational arc with a clear payoff.", focus: "Structure · meaning · payoff", color: "violet", icon: Film, tools: ["analyze-story-structure", "transcribe-bilingual"] },
  { id: "hook-scout", name: "Retention Editor", detail: "Finds the strongest opening and tightens the pace without clickbait.", focus: "Hook · pacing · attention", color: "blue", icon: Scissors, tools: ["analyze-retention", "render-vertical-short"] },
  { id: "creator-advocate", name: "Style Editor", detail: "Applies your approved taste to captions, framing, and visual rhythm.", focus: "Taste · captions · framing", color: "pink", icon: WandSparkles, tools: ["apply-creator-taste", "overlay-timed-image", "design-thumbnail"] },
  { id: "context-guardian", name: "Continuity Editor", detail: "Protects caveats, context, and natural sentence boundaries.", focus: "Context · accuracy · endings", color: "coral", icon: ShieldCheck, tools: ["validate-context-continuity", "render-long-form"] },
] as const;

const INITIAL_AGENT_STATES = Object.fromEntries(
  AGENTS.map((agent) => [agent.id, { status: "ready" as AgentLaneStatus, detail: "Waiting for a governed fan-out plan" }]),
);

function formatDuration(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds % 60);
  return `${minutes}:${remaining.toString().padStart(2, "0")}`;
}

function nowLabel() {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date());
}

function pathToUrl(path?: string | null) {
  if (!path) return undefined;
  return path.startsWith("data:") ? path : convertFileSrc(path);
}

function isChatImagePath(path: string) {
  return /\.(png|jpe?g|webp|gif)$/i.test(path);
}

function fileNameFromPath(path: string) {
  return path.split(/[\\/]/).pop() || "image";
}

function formatMediaTime(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = Math.floor(seconds % 60);
  return hours
    ? `${hours}:${minutes.toString().padStart(2, "0")}:${remaining.toString().padStart(2, "0")}`
    : `${minutes}:${remaining.toString().padStart(2, "0")}`;
}

function timestampTokens(value: string) {
  return Array.from(value.matchAll(/@(\d+(?::\d{1,2})?:\d{2})\b/g), (match) => {
    const parts = match[1].split(":").map(Number);
    const seconds = parts.length === 3
      ? parts[0] * 3600 + parts[1] * 60 + parts[2]
      : parts[0] * 60 + parts[1];
    return { label: match[1], seconds };
  });
}

function LocalVideo({
  path,
  poster,
  autoPlay = false,
  onPlayheadChange,
  onAddTimestamp,
}: {
  path: string;
  poster?: string;
  autoPlay?: boolean;
  onPlayheadChange?: (seconds: number) => void;
  onAddTimestamp?: (seconds: number) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [hoveredTime, setHoveredTime] = useState<number | null>(null);
  const [hoveredPercent, setHoveredPercent] = useState(0);
  const [videoFullscreen, setVideoFullscreen] = useState(false);
  const [fullscreenBusy, setFullscreenBusy] = useState(false);
  const videoFullscreenRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setSource(null);
    setError(null);
    setPlaying(false);
    setCurrentTime(0);
    setDuration(0);
    setHoveredTime(null);
    onPlayheadChange?.(0);
    void prepareMediaPreview(path)
      .then((url) => {
        if (!cancelled) setSource(url);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [onPlayheadChange, path]);

  useEffect(() => {
    videoFullscreenRef.current = videoFullscreen;
    document.body.classList.toggle("reelbrain-video-fullscreen", videoFullscreen);
    return () => {
      document.body.classList.remove("reelbrain-video-fullscreen");
    };
  }, [videoFullscreen]);

  useEffect(() => {
    let disposeResize: (() => void) | undefined;
    const appWindow = getCurrentWindow();
    const syncFullscreenExit = async () => {
      if (!videoFullscreenRef.current) return;
      const nativeFullscreen = await appWindow.isFullscreen().catch(() => false);
      if (!nativeFullscreen && !document.fullscreenElement) setVideoFullscreen(false);
    };
    void appWindow.onResized(() => void syncFullscreenExit()).then((dispose) => {
      disposeResize = dispose;
    });
    const handleDocumentFullscreen = () => {
      if (videoFullscreenRef.current && !document.fullscreenElement) void syncFullscreenExit();
    };
    document.addEventListener("fullscreenchange", handleDocumentFullscreen);
    return () => {
      disposeResize?.();
      document.removeEventListener("fullscreenchange", handleDocumentFullscreen);
    };
  }, []);

  function togglePlayback() {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) void video.play();
    else video.pause();
  }

  function seekBy(seconds: number) {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + seconds));
  }

  function toggleMute() {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
  }

  async function enterPictureInPicture() {
    const video = videoRef.current;
    if (video && document.pictureInPictureEnabled && document.pictureInPictureElement !== video) {
      await video.requestPictureInPicture();
    }
  }

  async function toggleFullscreen() {
    if (fullscreenBusy) return;
    setFullscreenBusy(true);
    const appWindow = getCurrentWindow();
    try {
      if (videoFullscreen) {
        if (document.fullscreenElement) await document.exitFullscreen();
        if (await appWindow.isFullscreen().catch(() => false)) await appWindow.setFullscreen(false);
        setVideoFullscreen(false);
        return;
      }
      try {
        await appWindow.setFullscreen(true);
        setVideoFullscreen(true);
      } catch (nativeError) {
        if (!surfaceRef.current?.requestFullscreen) throw nativeError;
        await surfaceRef.current.requestFullscreen();
        setVideoFullscreen(true);
      }
    } finally {
      setFullscreenBusy(false);
    }
  }

  return (
    <div className={`local-video ${videoFullscreen ? "is-video-fullscreen" : ""}`} ref={surfaceRef}>
      {error ? (
        <div className="video-empty video-error"><Film size={38} /><strong>Video preview failed</strong><span>{error}</span></div>
      ) : !source ? (
        <div className="video-empty"><LoaderCircle className="spin" /><strong>Preparing local preview…</strong><span>The video remains on this device.</span></div>
      ) : (
        <>
          {poster && <div className="video-backdrop" style={{ backgroundImage: `url(${pathToUrl(poster)})` }} aria-hidden="true" />}
          <video
            ref={videoRef}
            key={source}
            autoPlay={autoPlay}
            playsInline
            poster={poster ? pathToUrl(poster) : undefined}
            src={source}
            onClick={togglePlayback}
            onDoubleClick={() => void toggleFullscreen()}
            onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)}
            onDurationChange={(event) => setDuration(event.currentTarget.duration)}
            onTimeUpdate={(event) => {
              const nextTime = event.currentTarget.currentTime;
              setCurrentTime(nextTime);
              onPlayheadChange?.(nextTime);
            }}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onVolumeChange={(event) => {
              setVolume(event.currentTarget.volume);
              setMuted(event.currentTarget.muted);
            }}
            onError={(event) => {
              const media = event.currentTarget;
              setError(media.error?.message || `This video could not be decoded (media error ${media.error?.code ?? "unknown"}).`);
            }}
          />
          <div className="video-controls" role="group" aria-label="Video controls">
            <div className="video-control-row">
              <button className="video-play" onClick={togglePlayback} aria-label={playing ? "Pause video" : "Play video"}>
                {playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}
              </button>
              <button onClick={() => seekBy(-10)} aria-label="Go back 10 seconds"><SkipBack size={17} /></button>
              <button onClick={() => seekBy(10)} aria-label="Go forward 10 seconds"><SkipForward size={17} /></button>
              <time>{formatMediaTime(currentTime)}</time>
              <div
                className="video-progress-wrap"
                onPointerMove={(event) => {
                  if (!duration || (event.target as HTMLElement).closest(".timestamp-add-button")) return;
                  const bounds = event.currentTarget.getBoundingClientRect();
                  const percent = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
                  setHoveredPercent(percent * 100);
                  setHoveredTime(percent * duration);
                }}
                onPointerLeave={() => setHoveredTime(null)}
              >
                <input
                  className="video-progress"
                  type="range"
                  min="0"
                  max={duration || 0}
                  step="0.05"
                  value={Math.min(currentTime, duration || 0)}
                  aria-label="Video position"
                  style={{ "--video-progress": `${duration ? (currentTime / duration) * 100 : 0}%` } as React.CSSProperties}
                  onChange={(event) => {
                    const nextTime = Number(event.currentTarget.value);
                    if (videoRef.current) videoRef.current.currentTime = nextTime;
                    setCurrentTime(nextTime);
                    onPlayheadChange?.(nextTime);
                  }}
                />
                {hoveredTime !== null && onAddTimestamp && (
                  <button
                    type="button"
                    className="timestamp-add-button"
                    style={{ left: `${hoveredPercent}%` }}
                    aria-label={`Add ${formatMediaTime(hoveredTime)} to chat`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onAddTimestamp(hoveredTime);
                      setHoveredTime(null);
                    }}
                  >
                    <Clock3 size={11} /><strong>@{formatMediaTime(hoveredTime)}</strong><span>Add to chat</span>
                  </button>
                )}
              </div>
              <time>{formatMediaTime(duration)}</time>
              <button onClick={toggleMute} aria-label={muted ? "Unmute video" : "Mute video"}>{muted || volume === 0 ? <VolumeX size={17} /> : <Volume2 size={17} />}</button>
              <input
                className="video-volume"
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={muted ? 0 : volume}
                aria-label="Video volume"
                onChange={(event) => {
                  const nextVolume = Number(event.currentTarget.value);
                  if (videoRef.current) {
                    videoRef.current.volume = nextVolume;
                    videoRef.current.muted = nextVolume === 0;
                  }
                  setVolume(nextVolume);
                  setMuted(nextVolume === 0);
                }}
              />
              {document.pictureInPictureEnabled && <button onClick={() => void enterPictureInPicture()} aria-label="Picture in picture"><PictureInPicture2 size={17} /></button>}
              <button disabled={fullscreenBusy} onClick={() => void toggleFullscreen()} aria-label={videoFullscreen ? "Exit fullscreen" : "Enter fullscreen"} title={videoFullscreen ? "Exit fullscreen" : "Enter fullscreen"}>
                {videoFullscreen ? <Minimize2 size={17} /> : <Maximize2 size={17} />}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function scopeLabel(scope: PreferenceScope) {
  const values = [scope.outputMode, scope.contentKind, scope.language].filter(Boolean);
  return values.length ? values.join(" · ") : "All relevant edits";
}

function friendlyEvent(value?: string) {
  return (value || "event").replaceAll("_", " ");
}

function evidenceDate(value?: string) {
  if (!value) return null;
  const numeric = Number(value);
  const date = Number.isFinite(numeric) && /^\d+$/u.test(value) ? new Date(numeric) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function evidenceDateLabel(value?: string) {
  return evidenceDate(value)?.toLocaleString() || "Local evidence";
}

function mentionSlug(value: string) {
  return value
    .toLocaleLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-|-$/g, "");
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function mergeActivities(current: ChatActivity[], incoming: ChatActivity[]) {
  const merged = new Map(current.map((activity) => [`${activity.actor}:${activity.id}`, activity]));
  incoming.forEach((activity) => merged.set(`${activity.actor}:${activity.id}`, activity));
  return Array.from(merged.values());
}

function ActivityIcon({ kind }: { kind: string }) {
  if (kind === "command") return <Terminal size={13} />;
  if (kind === "file") return <FilePenLine size={13} />;
  if (kind === "search") return <Search size={13} />;
  if (kind === "team" || kind === "route") return <Bot size={13} />;
  return <Wrench size={13} />;
}

function RichText({ children }: { children: string }) {
  return (
    <div className="rich-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children: tableChildren }) => <div className="rich-table-scroll"><table>{tableChildren}</table></div>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function ActivityFeed({ activities, live = false }: { activities: ChatActivity[]; live?: boolean }) {
  if (!activities.length) return null;
  const activeCount = activities.filter((activity) => activity.status === "running").length;
  return (
    <details className={`activity-feed ${live ? "is-live" : ""}`} open={live || undefined}>
      <summary>
        <span className="activity-summary-icon"><Wrench size={12} /></span>
        <strong>{activeCount ? `${activeCount} active` : `${activities.length} agent step${activities.length === 1 ? "" : "s"}`}</strong>
        <ChevronDown size={13} />
      </summary>
      <div className="activity-list">
        {activities.map((activity) => (
          <article className={`activity-row activity-row--${activity.status}`} key={`${activity.actor}:${activity.id}`}>
            <span className="activity-kind"><ActivityIcon kind={activity.kind} /></span>
            <div><strong>{activity.title}</strong><small>{activity.actor}{activity.detail ? ` · ${activity.detail}` : ""}</small></div>
            <span className="activity-status" aria-label={activity.status}>{activity.status === "running" ? <LoaderCircle className="spin" size={12} /> : activity.status === "completed" ? <Check size={12} /> : <X size={12} />}</span>
          </article>
        ))}
      </div>
    </details>
  );
}

function WorkflowProgressCard({
  workflow,
  busy = false,
  onApprove,
  onDismiss,
}: {
  workflow: WorkflowProgress;
  busy?: boolean;
  onApprove?: () => void;
  onDismiss?: () => void;
}) {
  const progress = Math.max(0, Math.min(100, Math.round(workflow.progress)));
  const statusLabel = workflow.status === "awaiting_approval"
    ? "Approval required"
    : workflow.status === "running"
      ? workflow.phase
      : workflow.status === "completed"
        ? "Complete"
        : workflow.status === "blocked"
          ? "Stopped"
          : "Failed";
  return (
    <section className={`workflow-progress-card workflow-progress-card--${workflow.status}`} aria-live="polite">
      <header>
        <span className="workflow-progress-icon">
          {workflow.status === "running" ? <LoaderCircle className="spin" size={14} /> : workflow.status === "completed" ? <CheckCircle2 size={14} /> : workflow.status === "failed" ? <X size={14} /> : <Activity size={14} />}
        </span>
        <div><small>{statusLabel}</small><strong>{workflow.title}</strong></div>
        <em>{progress}%</em>
      </header>
      <div className="workflow-progress-track" role="progressbar" aria-label={workflow.title} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
        <i style={{ width: `${progress}%` }} />
      </div>
      <p>{workflow.detail}</p>
      <footer>
        <span className={workflow.videoChanged ? "did-change" : "no-change"}>{workflow.videoChanged ? "New video draft created" : "Video unchanged"}</span>
        {workflow.status === "awaiting_approval" && onApprove && (
          <div>
            {onDismiss && <button disabled={busy} onClick={onDismiss}>No</button>}
            <button className="workflow-approve" disabled={busy} onClick={onApprove}>{busy ? <LoaderCircle className="spin" size={12} /> : <Check size={12} />} Yes</button>
          </div>
        )}
      </footer>
    </section>
  );
}

type DislikeReasonDraft = {
  wrong: string;
  change: string;
  preserve: string;
};

function DraftFeedbackCard({
  output,
  busy,
  expanded,
  reason,
  onReasonChange,
  onLike,
  onSkip,
  onDislike,
  onSubmitDislike,
  onView,
}: {
  output: ReviewOutput;
  busy: boolean;
  expanded: boolean;
  reason: DislikeReasonDraft;
  onReasonChange: (reason: DislikeReasonDraft) => void;
  onLike: () => void;
  onSkip: () => void;
  onDislike: () => void;
  onSubmitDislike: () => void;
  onView?: () => void;
}) {
  const pending = output.feedbackStatus === "pending";
  if (!pending) {
    return (
      <section className={`draft-feedback-card is-${output.feedbackStatus}`}>
        <header><span>{output.feedbackStatus === "liked" ? <ThumbsUp size={15} /> : output.feedbackStatus === "skipped" ? <SkipForward size={15} /> : <ThumbsDown size={15} />}</span><div><small>{output.feedbackStatus === "skipped" ? "Feedback skipped" : "Feedback recorded"}</small><strong>Draft v{output.version} was {output.feedbackStatus}.</strong></div></header>
        <p>{output.feedbackStatus === "liked" ? "This choice is now a creator-approved behavioral prior with durable provenance." : output.feedbackStatus === "skipped" ? "ReelBrain saved a tentative taste episode from your original request. It is not active taste until consistent evidence is confirmed." : "Your correction was saved as taste evidence and this version left the active Review queue."}</p>
      </section>
    );
  }
  const canSubmit = reason.wrong.trim().length > 2 && reason.change.trim().length > 2;
  return (
    <section className={`draft-feedback-card ${expanded ? "is-expanded" : ""}`}>
      <header>
        <span><Sparkles size={15} /></span>
        <div><small>New version ready · v{output.version}</small><strong>Does this edit feel like you?</strong></div>
        {onView && <button className="draft-feedback-view" onClick={onView}>View draft</button>}
      </header>
      <p>{output.revisionSummary || output.rationale}</p>
      {!expanded ? (
        <div className="draft-feedback-actions">
          <button disabled={busy} className="dislike" onClick={onDislike}><ThumbsDown size={15} /> Dislike</button>
          <button disabled={busy} className="skip" onClick={onSkip}><SkipForward size={15} /> Skip</button>
          <button disabled={busy} className="like" onClick={onLike}>{busy ? <LoaderCircle className="spin" size={14} /> : <ThumbsUp size={15} />} Like</button>
        </div>
      ) : (
        <div className="draft-feedback-reason">
          <div><span>Help ReelBrain understand the mismatch</span><small>Your answers become inspectable taste evidence and the instruction for v{output.version + 1}.</small></div>
          <label><span>What feels wrong?</span><textarea autoFocus value={reason.wrong} onChange={(event) => onReasonChange({ ...reason, wrong: event.target.value })} placeholder="For example: the opening is too slow and the first payoff arrives late." /></label>
          <label><span>What should the next draft do instead?</span><textarea value={reason.change} onChange={(event) => onReasonChange({ ...reason, change: event.target.value })} placeholder="For example: open on the surprising conclusion, then explain why." /></label>
          <label><span>What must it preserve?</span><input value={reason.preserve} onChange={(event) => onReasonChange({ ...reason, preserve: event.target.value })} placeholder="Optional · a caveat, caption style, pacing moment…" /></label>
          <div className="draft-feedback-actions">
            <button disabled={busy} onClick={onDislike}>Back</button>
            <button disabled={busy || !canSubmit} className="like" onClick={onSubmitDislike}>{busy ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />} Make another draft</button>
          </div>
        </div>
      )}
    </section>
  );
}

function ToolApprovalCard({
  request,
  busy,
  onAction,
}: {
  request: ToolApprovalRequest;
  busy: boolean;
  onAction: (action: "approve" | "deny" | "deploy") => void;
}) {
  const pending = request.status === "pending_creator_approval";
  const building = request.status === "approved_for_quarantined_build" || request.status === "building_quarantined_tool";
  const tested = request.status === "quarantined_pending_deploy_approval";
  const deployed = request.status === "deployed";
  const failed = request.status === "build_or_test_failed";
  const deploymentDenied = request.status === "deployment_denied_by_creator";
  return (
    <section className={`tool-approval-card tool-approval-card--${pending ? "pending" : tested || deployed ? "approved" : failed ? "failed" : building ? "building" : "denied"}`}>
      <header>
        <span><Wrench size={14} /></span>
        <div>
          <small>{pending ? "Approval required" : building ? "Building and testing" : tested ? "Independent tests passed" : deployed ? "Tool deployed" : failed ? "Build or test failed" : deploymentDenied ? "Deployment denied" : "Request denied"}</small>
          <strong>{request.toolName}</strong>
        </div>
        <em>{pending ? "Waiting for you" : building ? "Quarantined" : tested ? "Deploy gate" : deployed ? "Active" : deploymentDenied ? "Quarantined" : "Stopped"}</em>
      </header>
      <p>{request.purpose}</p>
      <div className="tool-approval-reason"><Info size={13} /><span><strong>Why a new tool?</strong>{request.reasonMissing}</span></div>
      <details>
        <summary>Review scope and effects <ChevronDown size={12} /></summary>
        <dl>
          <div><dt>Requested by</dt><dd>{request.requestedBy}</dd></div>
          <div><dt>Capabilities</dt><dd>{request.capabilities.join(", ") || "Not declared"}</dd></div>
          <div><dt>Dependencies</dt><dd>{request.dependencies.join(", ") || "None"}</dd></div>
          <div><dt>Permissions</dt><dd>{request.permissions.join(", ") || "None"}</dd></div>
          <div><dt>Data effects</dt><dd>{request.dataEffects.join(", ") || "None"}</dd></div>
        </dl>
      </details>
      {pending ? (
        <>
          <p className="tool-approval-boundary"><ShieldCheck size={13} />Approval permits Toolsmith to build in quarantine and an independent Auditor to execute bounded tests. Deployment and creator-data execution remain blocked.</p>
          <div className="tool-approval-actions">
            <button className="tool-deny" disabled={busy} onClick={() => onAction("deny")}>No</button>
            <button className="tool-approve" disabled={busy} onClick={() => onAction("approve")}>
              {busy ? <LoaderCircle size={13} className="spin" /> : <Check size={13} />} Yes
            </button>
          </div>
        </>
      ) : building ? (
        <p className="tool-approval-boundary"><LoaderCircle size={13} className="spin" />Toolsmith is creating the bounded artifact. Tool Auditor will run conformance tests before this state can advance.</p>
      ) : tested ? (
        <>
          <p className="tool-approval-boundary"><CheckCircle2 size={13} />{request.testSummary || "The quarantined tool passed its independent test suite."}</p>
          <div className="tool-approval-actions tool-approval-actions--deploy">
            <button className="tool-deny" disabled={busy} onClick={() => onAction("deny")}>No</button>
            <button className="tool-approve" disabled={busy} onClick={() => onAction("deploy")}>
              {busy ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />} Yes
            </button>
          </div>
        </>
      ) : deployed ? (
        <p className="tool-approval-boundary"><CheckCircle2 size={13} />Tested artifact deployed with digest {request.artifactDigest?.slice(0, 12) || "recorded"}. It remains governed by ACP capability checks.</p>
      ) : failed ? (
        <p className="tool-approval-boundary"><X size={13} />{request.testSummary || "The generated tool failed its independent audit and was not deployed."}</p>
      ) : deploymentDenied ? (
        <p className="tool-approval-boundary"><ShieldCheck size={13} />The tested artifact remains quarantined and inactive. It was not deployed or executed against creator data.</p>
      ) : (
        <p className="tool-approval-boundary"><X size={13} />No tool was created, installed, deployed, or executed.</p>
      )}
    </section>
  );
}

function MemoryEvidenceGraph({ preferences, evidenceCount }: { preferences: TastePreference[]; evidenceCount: number }) {
  const rows = preferences.slice(0, 5);
  const height = Math.max(330, 154 + rows.length * 76);
  const decisionY = rows.length ? 120 + (rows.length - 1) * 38 : height / 2;
  const shorten = (value: string, maximum: number) => value.length > maximum ? `${value.slice(0, maximum - 1)}…` : value;
  return (
    <div className="memory-evidence-graph">
      <svg viewBox={`0 0 1000 ${height}`} role="img" aria-labelledby="memory-graph-title memory-graph-description">
        <title id="memory-graph-title">Creator memory provenance and behavioral influence</title>
        <desc id="memory-graph-description">Feedback provenance records create inspectable memory entries. Active memory can influence future editing behavior but never acts as source evidence.</desc>
        <defs>
          <linearGradient id="memory-surface" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#b9a4ff" stopOpacity="0.16" />
            <stop offset="0.52" stopColor="#8b5cf6" stopOpacity="0.1" />
            <stop offset="1" stopColor="#6d46cc" stopOpacity="0.04" />
          </linearGradient>
          <linearGradient id="provenance-flow" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#62a7ff" stopOpacity="0.24" />
            <stop offset="1" stopColor="#9f8cff" stopOpacity="0.72" />
          </linearGradient>
          <linearGradient id="behavior-flow" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#ad8cff" stopOpacity="0.42" />
            <stop offset="1" stopColor="#8ad7bb" stopOpacity="0.7" />
          </linearGradient>
          <radialGradient id="decision-core" cx="42%" cy="35%" r="72%">
            <stop offset="0" stopColor="#8ce0bd" stopOpacity="0.19" />
            <stop offset="0.62" stopColor="#4cae82" stopOpacity="0.09" />
            <stop offset="1" stopColor="#173e31" stopOpacity="0.02" />
          </radialGradient>
          <filter id="memory-soft-shadow" x="-20%" y="-30%" width="140%" height="170%">
            <feDropShadow dx="0" dy="8" stdDeviation="12" floodColor="#000000" floodOpacity="0.24" />
          </filter>
          <filter id="memory-point-glow" x="-250%" y="-250%" width="600%" height="600%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <marker id="provenance-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
          <marker id="behavior-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
        </defs>
        <rect className="graph-prior-field" x="315" y="76" width="440" height={height - 112} rx="32" />

        <g className="graph-stage-heading graph-stage-heading--source">
          <circle cx="34" cy="29" r="12" /><text className="graph-stage-number" x="34" y="33" textAnchor="middle">1</text>
          <text className="graph-column-label" x="54" y="27">Evidence ledger</text>
          <text className="graph-column-detail" x="54" y="47">{evidenceCount} durable event{evidenceCount === 1 ? "" : "s"} · inspectable provenance</text>
        </g>
        <g className="graph-stage-heading graph-stage-heading--memory">
          <circle cx="340" cy="29" r="12" /><text className="graph-stage-number" x="340" y="33" textAnchor="middle">2</text>
          <text className="graph-column-label" x="360" y="27">Remembered behavior prior</text>
          <text className="graph-column-detail" x="360" y="47">Creator-controlled · correctable · deletable</text>
        </g>
        <g className="graph-stage-heading graph-stage-heading--future">
          <circle cx="790" cy="29" r="12" /><text className="graph-stage-number" x="790" y="33" textAnchor="middle">3</text>
          <text className="graph-column-label" x="810" y="27">Edit-time influence</text>
          <text className="graph-column-detail" x="810" y="47">Applied only when relevant</text>
        </g>

        {rows.length ? rows.map((preference, index) => {
          const y = 94 + index * 76;
          const active = preference.status === "active";
          return (
            <g className={`graph-memory-row ${active ? "is-active" : "is-disabled"}`} key={preference.id}>
              <path className="graph-link graph-link--provenance" d={`M 269 ${y + 26} C 299 ${y + 26}, 308 ${y + 26}, 340 ${y + 26}`} markerEnd="url(#provenance-arrow)" />
              <path className="graph-link graph-link--behavior" d={`M 732 ${y + 26} C 780 ${y + 26}, 785 ${decisionY}, 806 ${decisionY}`} markerEnd="url(#behavior-arrow)" />
              <g className="graph-node graph-node--provenance">
                <rect x="34" y={y} width="235" height="52" rx="16" />
                <circle className="graph-record-count" cx="59" cy={y + 26} r="13" />
                <text className="graph-record-number" x="59" y={y + 30} textAnchor="middle">{preference.provenanceEventIds.length}</text>
                <text x="81" y={y + 22}>{shorten(preference.category, 24)}</text>
                <text className="graph-node-detail" x="81" y={y + 39}>linked feedback record{preference.provenanceEventIds.length === 1 ? "" : "s"}</text>
              </g>
              <g className="graph-node graph-node--memory" filter="url(#memory-soft-shadow)">
                <rect x="340" y={y - 3} width="392" height="58" rx="18" />
                <circle className="graph-memory-point" cx="365" cy={y + 26} r="5" filter="url(#memory-point-glow)" />
                <text x="386" y={y + 21}>{shorten(preference.value, 46)}</text>
                <text className="graph-node-detail" x="386" y={y + 41}>{shorten(scopeLabel(preference.scope), 32)} · {active ? "active" : "paused"}</text>
              </g>
            </g>
          );
        }) : (
          <g className="graph-empty">
            <circle cx="535" cy={height / 2 - 12} r="5" />
            <text x="535" y={height / 2 + 18} textAnchor="middle">No remembered preferences yet</text>
          </g>
        )}

        <g className="graph-decision-field">
          <circle className="graph-decision-orbit graph-decision-orbit--outer" cx="875" cy={decisionY} r="68" />
          <circle className="graph-decision-orbit graph-decision-orbit--inner" cx="875" cy={decisionY} r="51" />
          <circle className="graph-decision-core" cx="875" cy={decisionY} r="43" />
          <circle className="graph-orbit-point graph-orbit-point--style" cx="826" cy={decisionY - 47} r="3" />
          <circle className="graph-orbit-point graph-orbit-point--pace" cx="939" cy={decisionY - 22} r="3" />
          <circle className="graph-orbit-point graph-orbit-point--frame" cx="912" cy={decisionY + 57} r="3" />
          <text className="graph-orbit-label" x="805" y={decisionY - 57}>style</text>
          <text className="graph-orbit-label" x="943" y={decisionY - 27}>pace</text>
          <text className="graph-orbit-label" x="919" y={decisionY + 67}>frame</text>
          <text className="graph-decision-title" x="875" y={decisionY - 5} textAnchor="middle">Next edit</text>
          <text className="graph-decision-subtitle" x="875" y={decisionY + 14} textAnchor="middle">choices</text>
          <text className="graph-decision-warning" x="875" y={decisionY + 31} textAnchor="middle">prior, never proof</text>
        </g>
      </svg>
      <div className="memory-graph-legend">
        <span><i />Feedback established this preference</span>
        <span><i className="prior" />Preference may shape a future choice</span>
        <span className="memory-graph-principle"><ShieldCheck size={12} /> Memory guides behavior, never facts</span>
      </div>
    </div>
  );
}

function App() {
  const [activeView, setActiveView] = useState<ViewName>("Projects");
  const [connection, setConnection] = useState(DEFAULT_STATUS);
  const [run, setRun] = useState<ReviewRun>({
    available: false,
    status: "NO_RUN",
    projectTitle: "Untitled local project",
    outputs: [],
  });
  const [memory, setMemory] = useState<MemoryState | null>(null);
  const [evidence, setEvidence] = useState<EvidenceState>({ fanouts: [], events: [], reviewEvents: [] });
  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [selectedOutputId, setSelectedOutputId] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<VideoPreflight | null>(null);
  const [dropBusy, setDropBusy] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatActivity, setChatActivity] = useState<string | null>(null);
  const [chatActivities, setChatActivities] = useState<ChatActivity[]>([]);
  const [chatAttachments, setChatAttachments] = useState<ChatAttachment[]>([]);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [chatDropActive, setChatDropActive] = useState(false);
  const [mentionSelection, setMentionSelection] = useState(0);
  const [mentionMenuDismissed, setMentionMenuDismissed] = useState(false);
  const [toolApprovalBusy, setToolApprovalBusy] = useState<string | null>(null);
  const [videoPlayhead, setVideoPlayhead] = useState(0);
  const [timestampMentions, setTimestampMentions] = useState<number[]>([]);
  const [chatWidth, setChatWidth] = useState(() => {
    const stored = Number(window.localStorage.getItem("reelbrain.chat-width"));
    return Number.isFinite(stored) && stored >= 272 && stored <= 544 ? stored : 320;
  });
  const [chatResizing, setChatResizing] = useState(false);
  const [fanoutBusy, setFanoutBusy] = useState(false);
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [pendingRevision, setPendingRevision] = useState<PendingRevision | null>(null);
  const [feedbackBusy, setFeedbackBusy] = useState<string | null>(null);
  const [dislikeTarget, setDislikeTarget] = useState<string | null>(null);
  const [dislikeReasons, setDislikeReasons] = useState<Record<string, DislikeReasonDraft>>({});
  const [activeWorkflow, setActiveWorkflow] = useState<WorkflowProgress | null>(null);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [personaThreadIds, setPersonaThreadIds] = useState<Record<string, string>>({});
  const [fanout, setFanout] = useState<FanoutResult | null>(null);
  const [, setAgentStates] = useState(INITIAL_AGENT_STATES);
  const [videoPreviewActive, setVideoPreviewActive] = useState(false);
  const [agentProfiles, setAgentProfiles] = useState<AgentProfileState | null>(null);
  const [agentEditorDraft, setAgentEditorDraft] = useState<AgentProfile | null>(null);
  const [agentSaving, setAgentSaving] = useState(false);
  const [tasteEditorDraft, setTasteEditorDraft] = useState<TastePreference | null>(null);
  const [tasteForgetTarget, setTasteForgetTarget] = useState<TastePreference | null>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const chatScrollRef = useRef({ top: 0, followBottom: true });
  const activeChatRequestRef = useRef<string | null>(null);
  const chatActivitiesRef = useRef<ChatActivity[]>([]);
  const chatResizeRef = useRef({ startX: 0, startWidth: 320 });
  const fanoutWorkflowIdRef = useRef<string | null>(null);
  const revisionWorkflowByJobRef = useRef<Record<string, string>>({});
  const fanoutAgentStatusesRef = useRef<Record<string, AgentLaneStatus>>({});
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "system-architecture",
      role: "system",
      text: "The Showrunner handles ordinary chat. Mention one editor for a direct response, or @team to consult all four editors and synthesize their recommendations.",
      time: nowLabel(),
    },
  ]);

  function updateWorkflowMessage(workflowId: string, patch: Partial<WorkflowProgress>) {
    setMessages((current) => current.map((message) => (
      message.workflow?.id === workflowId
        ? { ...message, workflow: { ...message.workflow, ...patch } }
        : message
    )));
  }

  function dismissPendingRevision(workflowId: string) {
    updateWorkflowMessage(workflowId, {
      status: "blocked",
      phase: "Not approved",
      detail: "The proposal remains in chat. No revision or render effect was started.",
      videoChanged: false,
    });
    setPendingRevision((current) => current?.workflowId === workflowId ? null : current);
  }

  const refreshConnection = useCallback(async () => {
    try {
      setConnection(await readCodexStatus());
    } catch (error) {
      setConnection({
        connected: false,
        authMode: null,
        requiresOpenaiAuth: true,
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem("reelbrain.chat-width", String(Math.round(chatWidth)));
  }, [chatWidth]);

  const handleVideoPlayheadChange = useCallback((seconds: number) => {
    setVideoPlayhead(Math.max(0, Math.floor(seconds)));
  }, []);

  const addTimestampMention = useCallback((seconds: number) => {
    const timestamp = Math.max(0, Math.floor(seconds));
    setTimestampMentions((current) => current.includes(timestamp) ? current : [...current, timestamp]);
    setMentionMenuDismissed(false);
    window.setTimeout(() => promptRef.current?.focus(), 0);
  }, []);

  function clampChatWidth(width: number) {
    const maximum = Math.min(544, Math.max(320, window.innerWidth * 0.46));
    return Math.max(272, Math.min(maximum, width));
  }

  function handleChatResizeStart(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    chatResizeRef.current = { startX: event.clientX, startWidth: chatWidth };
    setChatResizing(true);
  }

  function handleChatResizeMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!chatResizing) return;
    const next = chatResizeRef.current.startWidth + (chatResizeRef.current.startX - event.clientX);
    setChatWidth(clampChatWidth(next));
  }

  function handleChatResizeEnd(event: ReactPointerEvent<HTMLDivElement>) {
    if (!chatResizing) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setChatResizing(false);
  }

  function handleChatResizeKey(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home") return;
    event.preventDefault();
    if (event.key === "Home") setChatWidth(320);
    else setChatWidth((current) => clampChatWidth(current + (event.key === "ArrowLeft" ? 16 : -16)));
  }

  const refreshDurableState = useCallback(async () => {
    const [nextMemory, nextEvidence, nextHealth] = await Promise.all([
      inspectCreatorMemory(),
      inspectFanoutEvidence(),
      readRuntimeHealth(),
    ]);
    setMemory(nextMemory);
    setEvidence(nextEvidence);
    setHealth(nextHealth);
  }, []);

  const refreshReviewRun = useCallback(async (preferredOutputId?: string | null) => {
    const nextRun = await discoverReviewRun();
    setRun(nextRun);
    setSelectedOutputId((current) => {
      const preferred = preferredOutputId || current;
      return nextRun.outputs.some((output) => output.outputId === preferred)
        ? preferred
        : nextRun.outputs[0]?.outputId ?? null;
    });
    return nextRun;
  }, []);

  useEffect(() => {
    void refreshConnection();
    void refreshDurableState();
    void readAgentProfiles().then(setAgentProfiles).catch((error) => {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not load editing personas: ${String(error)}`, time: nowLabel() },
      ]);
    });
    void refreshReviewRun();
  }, [refreshConnection, refreshDurableState, refreshReviewRun]);

  useEffect(() => {
    let dispose: (() => void) | undefined;
    void listen<RevisionProgress>("revision-progress", (event) => {
      const progress = event.payload;
      const workflowId = revisionWorkflowByJobRef.current[progress.jobId];
      if (!workflowId) return;
      updateWorkflowMessage(workflowId, {
        status: progress.status,
        phase: progress.phase,
        progress: progress.progress,
        detail: progress.detail,
        videoChanged: progress.status === "completed",
        outputId: progress.outputId,
      });
    }).then((unlisten) => {
      dispose = unlisten;
    });
    return () => dispose?.();
  }, []);

  useEffect(() => {
    if (!agentEditorDraft) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !agentSaving) setAgentEditorDraft(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [agentEditorDraft, agentSaving]);

  useEffect(() => {
    if (!tasteEditorDraft && !tasteForgetTarget) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || memoryBusy) return;
      setTasteEditorDraft(null);
      setTasteForgetTarget(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [tasteEditorDraft, tasteForgetTarget, memoryBusy]);

  useEffect(() => {
    let dispose: (() => void) | undefined;
    void listen<AgentProgress>("fanout-progress", (event) => {
      const progress = event.payload;
      fanoutAgentStatusesRef.current = {
        ...fanoutAgentStatusesRef.current,
        [progress.persona]: progress.status,
      };
      setAgentStates((current) => ({
        ...current,
        [progress.persona]: { status: progress.status, detail: progress.detail, threadId: progress.threadId },
      }));
      const workflowId = fanoutWorkflowIdRef.current;
      if (workflowId) {
        const statuses = Object.values(fanoutAgentStatusesRef.current);
        const completed = statuses.filter((status) => status === "completed").length;
        const failed = statuses.filter((status) => status === "failed").length;
        const editor = AGENTS.find((agent) => agent.id === progress.persona)?.name || progress.persona;
        updateWorkflowMessage(workflowId, {
          status: failed ? "failed" : "running",
          phase: failed ? "Agent failed" : `${completed}/4 editors complete`,
          progress: Math.min(78, 18 + completed * 14 + (progress.status === "running" ? 7 : 0)),
          detail: `${editor}: ${progress.detail}`,
          videoChanged: false,
        });
      }
    }).then((unlisten) => {
      dispose = unlisten;
    });
    return () => dispose?.();
  }, []);

  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (!list) return;
    if (chatScrollRef.current.followBottom) {
      list.scrollTop = list.scrollHeight;
      chatScrollRef.current.top = list.scrollTop;
    } else {
      list.scrollTop = chatScrollRef.current.top;
    }
  }, [messages, chatActivities, chatBusy]);

  useEffect(() => {
    let dispose: (() => void) | undefined;
    void listen<ChatActivityEvent>("chat-activity", (event) => {
      if (event.payload.requestId !== activeChatRequestRef.current) return;
      const next = mergeActivities(chatActivitiesRef.current, [event.payload.activity]);
      chatActivitiesRef.current = next;
      setChatActivities(next);
      setActiveWorkflow((current) => {
        if (!current) return current;
        const consultations = next.filter((activity) => activity.kind === "team" && activity.title.startsWith("Consult "));
        const completed = consultations.filter((activity) => activity.status === "completed").length;
        const failed = consultations.filter((activity) => activity.status === "failed").length;
        const synthesis = next.find((activity) => activity.id === "team-synthesis");
        const progress = synthesis?.status === "completed"
          ? 96
          : synthesis?.status === "running"
            ? 82
            : Math.min(74, 12 + completed * 15 + (consultations.some((activity) => activity.status === "running") ? 6 : 0));
        return {
          ...current,
          status: failed ? "failed" : "running",
          phase: synthesis ? "Showrunner synthesis" : `${completed}/4 editors complete`,
          progress,
          detail: synthesis?.status === "running"
            ? "The Showrunner is resolving disagreements into one revision proposal."
            : `${completed} of 4 independent editing perspectives have finished.`,
        };
      });
    }).then((unlisten) => {
      dispose = unlisten;
    });
    return () => dispose?.();
  }, []);

  const handlePath = useCallback(async (path: string) => {
    setDropBusy(true);
    try {
      const result = await preflightVideo(path);
      setPreflight(result);
      setActiveView("Projects");
      setAgentStates(INITIAL_AGENT_STATES);
      setFanout(null);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          text:
            result.status === "ready"
              ? `${result.name} passed local preflight. Source bytes have not been uploaded and no provider spend occurred.`
              : result.message,
          time: nowLabel(),
        },
      ]);
    } finally {
      setDropBusy(false);
    }
  }, []);

  const attachChatImagePath = useCallback(async (path: string) => {
    if (!isChatImagePath(path)) return;
    const previewUrl = await prepareChatImage(path);
    setChatAttachments((current) => {
      if (current.length >= 4 || current.some((attachment) => attachment.path === path)) return current;
      return [...current, { id: crypto.randomUUID(), path, name: fileNameFromPath(path), previewUrl }];
    });
  }, []);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void getCurrentWebview()
      .onDragDropEvent((event) => {
        if (event.payload.type === "leave") {
          setChatDropActive(false);
          return;
        }
        if (event.payload.type === "over") return;
        const paths = event.payload.paths;
        const imagePaths = paths.filter(isChatImagePath);
        if (event.payload.type === "enter") {
          setChatDropActive(imagePaths.length > 0);
          return;
        }
        if (event.payload.type === "drop") {
          setChatDropActive(false);
          if (imagePaths.length) {
            setActiveView("Projects");
            setAttachmentBusy(true);
            void Promise.all(imagePaths.slice(0, 4).map(attachChatImagePath))
              .catch((error) => {
                setMessages((current) => [
                  ...current,
                  { id: crypto.randomUUID(), role: "system", text: `Could not attach dropped image: ${String(error)}`, time: nowLabel() },
                ]);
              })
              .finally(() => {
                setAttachmentBusy(false);
                window.setTimeout(() => promptRef.current?.focus(), 0);
              });
          } else if (paths[0]) {
            void handlePath(paths[0]);
          }
        }
      })
      .then((dispose) => {
        unlisten = dispose;
      });
    return () => unlisten?.();
  }, [attachChatImagePath, handlePath]);

  const selectedOutput = useMemo<ReviewOutput | null>(
    () => run.outputs.find((output) => output.outputId === selectedOutputId) ?? run.outputs[0] ?? null,
    [run.outputs, selectedOutputId],
  );
  const selectedRootOutputId = selectedOutput?.baseOutputId || selectedOutput?.outputId || null;
  const selectedVersionFamily = useMemo(
    () => selectedRootOutputId
      ? run.outputs
          .filter((output) => (output.baseOutputId || output.outputId) === selectedRootOutputId)
          .sort((left, right) => left.version - right.version)
      : [],
    [run.outputs, selectedRootOutputId],
  );
  const pendingReviewOutputs = useMemo(
    () => run.outputs.filter((output) => output.feedbackStatus === "pending"),
    [run.outputs],
  );

  const configuredAgents = useMemo(
    () => AGENTS.map((agent) => ({
      ...agent,
      profile: agentProfiles?.profiles.find((profile) => profile.id === agent.id) ?? {
        id: agent.id,
        name: agent.name,
        role: agent.detail,
        systemPrompt: "",
      },
    })),
    [agentProfiles],
  );

  const mentionQuery = prompt.match(/^@([^\s]*)$/u)?.[1].toLocaleLowerCase() ?? null;
  const mentionSuggestions = useMemo(() => {
    if (mentionQuery === null) return [];
    const candidates = [
      {
        kind: "agent" as const,
        value: "team",
        label: "Team",
        detail: "Consult all four editors, then let the Showrunner synthesize.",
        aliases: ["team", "all", "everyone"],
      },
      ...configuredAgents.map(({ id, profile }) => ({
        kind: "agent" as const,
        value: mentionSlug(profile.name) || id,
        label: profile.name,
        detail: profile.role,
        aliases: [id, profile.name, mentionSlug(profile.name)],
      })),
      {
        kind: "timestamp" as const,
        value: formatMediaTime(videoPlayhead),
        label: `Mention timestamp ${formatMediaTime(videoPlayhead)}`,
        detail: "Add the current video’s local playhead to this chat.",
        aliases: ["time", "timestamp", formatMediaTime(videoPlayhead)],
      },
    ];
    return candidates.filter((candidate) => candidate.aliases.some((alias) => alias.toLocaleLowerCase().includes(mentionQuery)));
  }, [configuredAgents, mentionQuery, videoPlayhead]);
  const mentionMenuOpen = mentionSuggestions.length > 0 && !mentionMenuDismissed;

  useEffect(() => {
    setMentionSelection(0);
    setMentionMenuDismissed(false);
  }, [mentionQuery]);

  useEffect(() => {
    setVideoPreviewActive(false);
    setVideoPlayhead(0);
    setTimestampMentions([]);
  }, [activeView, selectedOutputId]);

  const runtimeLabel = connection.connected
    ? connection.authMode === "chatgpt"
      ? "Codex OAuth · root thread"
      : connection.authMode === "apiKey"
        ? "Codex API key · root thread"
        : "Codex connected · root thread"
    : connecting
      ? "Finish sign-in…"
      : "Connect Codex";

  async function chooseVideo() {
    const selected = await selectVideo();
    if (selected) await handlePath(selected);
  }

  async function handleConnect() {
    setConnecting(true);
    try {
      await connectCodex();
      const poll = window.setInterval(async () => {
        const status = await readCodexStatus();
        setConnection(status);
        if (status.connected) {
          window.clearInterval(poll);
          setConnecting(false);
        }
      }, 1800);
      window.setTimeout(() => {
        window.clearInterval(poll);
        setConnecting(false);
      }, 120_000);
    } catch (error) {
      setConnecting(false);
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Codex connection failed: ${String(error)}`, time: nowLabel() },
      ]);
    }
  }

  async function handleSend(text = prompt) {
    const trimmed = text.trim();
    const outgoingAttachments = [...chatAttachments];
    const outgoingTimestamps = [...timestampMentions];
    if ((!trimmed && !outgoingAttachments.length && !outgoingTimestamps.length) || chatBusy || attachmentBusy) return;
    const fallback = outgoingAttachments.length
      ? "Review the attached image and explain what should change."
      : "Review the mentioned moment and recommend the next edit.";
    const timestampContext = outgoingTimestamps.map((seconds) => `@${formatMediaTime(seconds)}`).join(" ");
    const creatorText = [trimmed || fallback, timestampContext].filter(Boolean).join(" ");
    const visibleDuration = preflight?.durationSeconds ?? selectedOutput?.durationSeconds;
    const invalidTimestamp = visibleDuration
      ? timestampTokens(creatorText).find(({ seconds }) => seconds > visibleDuration + 0.5)
      : undefined;
    if (invalidTimestamp && visibleDuration) {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          text: `@${invalidTimestamp.label} is outside this video. Its local timeline is 0:00–${formatMediaTime(visibleDuration)}.`,
          time: nowLabel(),
        },
      ]);
      return;
    }
    chatScrollRef.current.followBottom = true;
    setPrompt("");
    setChatAttachments([]);
    setTimestampMentions([]);
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "creator", text: creatorText, time: nowLabel(), attachments: outgoingAttachments },
    ]);
    if (!connection.connected) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: "Connect Codex first through the official browser flow.", time: nowLabel() },
      ]);
      return;
    }
    const requestId = crypto.randomUUID();
    activeChatRequestRef.current = requestId;
    chatActivitiesRef.current = [];
    setChatActivities([]);
    setChatBusy(true);
    try {
      const directAgent = configuredAgents.find(({ id, profile }) => {
        const aliases = [id, mentionSlug(profile.name)].filter(Boolean);
        return aliases.some((alias) => new RegExp(`^@${escapeRegExp(alias)}(?=\\s|$)`, "iu").test(creatorText));
      });
      const teamMentioned = /^@team\b/i.test(creatorText);
      const directAliases = directAgent ? [directAgent.id, mentionSlug(directAgent.profile.name)].filter(Boolean) : [];
      const cleanPrompt = creatorText
        .replace(/^@team\b\s*/i, "")
        .replace(directAgent ? new RegExp(`^@(?:${directAliases.map(escapeRegExp).join("|")})(?=\\s|$)\\s*`, "iu") : /$^/, "")
        .trim() || "Review the current edit and recommend the next revision.";
      const mediaContext = selectedOutput
        ? `Selected ${selectedOutput.mode} draft: “${selectedOutput.title}” (${formatDuration(selectedOutput.durationSeconds)}). The creator-facing timeline is draft-local and always runs from 0:00 to ${formatMediaTime(selectedOutput.durationSeconds)}. Interpret every creator timestamp, including @mentions, relative to this selected draft. Never expose the original source offset as a creator-facing timestamp. Current rationale: ${selectedOutput.rationale}. Caption status: ${selectedOutput.captionAccuracyStatus}. State: ${selectedOutput.status}.`
        : preflight
          ? `A creator-owned source passed local preflight at ${preflight.width ?? "?"}×${preflight.height ?? "?"}, duration ${preflight.durationSeconds ? formatDuration(preflight.durationSeconds) : "unknown"}. No render or provider effect is authorized.`
          : "No source or draft is selected.";
      const activeTaste = memory?.preferences.filter((preference) => preference.status === "active") ?? [];
      const tasteContext = activeTaste.length
        ? `Creator-approved behavioral priors (apply only when relevant; the current request overrides them):\n${activeTaste.map((preference) => `- ${preference.id} · ${preference.category}: ${preference.value} · scope: ${scopeLabel(preference.scope)}`).join("\n")}`
        : "No active creator-approved taste is available.";
      const projectContext = `${mediaContext}\n\n${tasteContext}\nMemory is a behavioral prior, never source evidence.`;
      const routingActivity: ChatActivity = directAgent
        ? {
            id: "routing-decision",
            actor: "ReelBrain Showrunner",
            kind: "route",
            title: `Route directly to ${directAgent.profile.name}`,
            detail: `You mentioned @${mentionSlug(directAgent.profile.name)}, so only this editor received the request.`,
            status: "completed",
          }
        : teamMentioned
          ? {
              id: "routing-decision",
              actor: "ReelBrain Showrunner",
              kind: "route",
              title: "Route to all four editors",
              detail: "You mentioned @team, so the Showrunner requested four independent perspectives before synthesis.",
              status: "completed",
            }
          : {
              id: "routing-decision",
              actor: "ReelBrain Showrunner",
              kind: "route",
              title: "Let the Showrunner decide",
              detail: "No explicit @mention was used. The Showrunner LLM may answer directly or orchestrate collaborators based on the request.",
              status: "completed",
            };
      chatActivitiesRef.current = [routingActivity];
      setChatActivities([routingActivity]);

      let response: string;
      let sender = "ReelBrain";
      let completedActivities: ChatActivity[] = [];
      let approvalRequest: ToolApprovalRequest | null | undefined;
      let completedWorkflow: WorkflowProgress | undefined;
      if (directAgent) {
        setChatActivity(`${directAgent.profile.name} is reviewing your request…`);
        const result = await chatWithAgent({
          personaId: directAgent.id,
          prompt: cleanPrompt,
          requestId,
          imagePaths: outgoingAttachments.map((attachment) => attachment.path),
          context: projectContext,
          threadId: personaThreadIds[directAgent.id],
        });
        setPersonaThreadIds((current) => ({ ...current, [directAgent.id]: result.threadId }));
        response = result.response;
        approvalRequest = result.approvalRequest;
        completedActivities = mergeActivities(chatActivitiesRef.current, result.activities || []);
        sender = directAgent.profile.name;
      } else if (teamMentioned) {
        setChatActivity("Showrunner is consulting all four editors…");
        setActiveWorkflow({
          id: `consultation-${requestId}`,
          title: "Preparing a revision proposal",
          phase: "Starting four editors",
          status: "running",
          progress: 8,
          detail: "Four independent editors are reviewing the selected draft and your current feedback.",
          videoChanged: false,
          outputId: selectedOutput?.outputId,
        });
        const result = await chatWithTeam({ prompt: cleanPrompt, requestId, imagePaths: outgoingAttachments.map((attachment) => attachment.path), context: projectContext, threadId });
        setThreadId(result.threadId);
        response = result.response;
        approvalRequest = result.approvalRequest;
        completedActivities = mergeActivities(chatActivitiesRef.current, result.activities || []);
        sender = "ReelBrain Showrunner";
        if (!approvalRequest) {
          const workflowId = `revision-${requestId}`;
          completedWorkflow = {
            id: workflowId,
            title: "Approve this revision request?",
            phase: "Awaiting creator approval",
            status: "awaiting_approval",
            progress: 100,
            detail: "The four editors finished their proposal. Approving records this revision request; the video remains unchanged until a renderer is separately started.",
            videoChanged: false,
            outputId: selectedOutput?.outputId,
          };
          if (pendingRevision) dismissPendingRevision(pendingRevision.workflowId);
          setPendingRevision({
            workflowId,
            creatorRequest: cleanPrompt,
            proposal: response,
            outputId: selectedOutput?.outputId,
          });
        }
      } else {
        setChatActivity("Showrunner is thinking…");
        const result = await chatWithReelBrain({ prompt: cleanPrompt, requestId, imagePaths: outgoingAttachments.map((attachment) => attachment.path), context: projectContext, threadId });
        setThreadId(result.threadId);
        response = result.response;
        approvalRequest = result.approvalRequest;
        completedActivities = mergeActivities(chatActivitiesRef.current, result.activities || []);
        if (!approvalRequest && result.revisionProposal) {
          const workflowId = `revision-${requestId}`;
          completedWorkflow = {
            id: workflowId,
            title: "Approve this revision request?",
            phase: "Awaiting creator decision",
            status: "awaiting_approval",
            progress: 100,
            detail: `${result.revisionProposal.summary} The video remains unchanged until an authorized renderer actually produces a new draft.`,
            videoChanged: false,
            outputId: selectedOutput?.outputId,
          };
          if (pendingRevision) dismissPendingRevision(pendingRevision.workflowId);
          setPendingRevision({
            workflowId,
            creatorRequest: cleanPrompt,
            proposal: response,
            outputId: selectedOutput?.outputId,
          });
        }
      }
      const responseTime = nowLabel();
      setMessages((current) => {
        const next: ChatMessage[] = [
          ...current,
          { id: crypto.randomUUID(), role: "reelbrain", sender, text: response, time: responseTime, activities: completedActivities, workflow: completedWorkflow },
        ];
        if (approvalRequest) {
          next.push({
            id: `approval-message-${approvalRequest.approvalId}`,
            role: "approval",
            sender: "ReelBrain Approval",
            text: "",
            time: responseTime,
            approvalRequest,
          });
        }
        return next;
      });
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `ReelBrain could not complete that turn: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      activeChatRequestRef.current = null;
      chatActivitiesRef.current = [];
      setChatBusy(false);
      setChatActivity(null);
      setChatActivities([]);
      setActiveWorkflow(null);
    }
  }

  async function approvePendingRevision(revision: PendingRevision) {
    if (revisionBusy || fanoutBusy) return;
    if (!revision.outputId) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "system", text: "Select a rendered draft before approving a revision.", time: nowLabel() }]);
      return;
    }
    setRevisionBusy(true);
    updateWorkflowMessage(revision.workflowId, {
      status: "running",
      phase: "Recording approval",
      progress: 18,
      detail: "Binding your approval to this draft and revision request.",
      videoChanged: false,
    });
    try {
      updateWorkflowMessage(revision.workflowId, {
        phase: "Recording revision request",
        progress: 10,
        detail: "Writing the creator decision to ReelBrain’s durable review evidence.",
      });
      await recordReviewAction({
        action: "revise",
        output_id: revision.outputId,
        creator_statement: `Approve this revision request: ${revision.creatorRequest}`,
        creator_id: "creator-founder",
        project_id: "founder-desktop-project",
      });
      setPendingRevision((current) => current?.workflowId === revision.workflowId ? null : current);
      await refreshDurableState();
      const jobId = `revision_${crypto.randomUUID().replaceAll("-", "_")}`;
      revisionWorkflowByJobRef.current[jobId] = revision.workflowId;
      updateWorkflowMessage(revision.workflowId, {
        status: "running",
        phase: "Planning render parameters",
        progress: 12,
        detail: "Approval recorded. The Style Editor is translating your request into bounded semantic-tool parameters.",
        videoChanged: false,
      });
      const baseOutput = run.outputs.find((output) => output.outputId === revision.outputId);
      if (!baseOutput) throw new Error("The selected draft is no longer available.");
      const renderPlan = await planRevision({
        instruction: revision.creatorRequest,
        mode: baseOutput.mode,
        durationSeconds: baseOutput.durationSeconds,
        title: baseOutput.title,
        rationale: baseOutput.rationale,
        requestId: jobId,
      });
      if (!renderPlan.supported) throw new Error(renderPlan.unsupportedReason || "This revision needs a tool capability the current renderer does not have.");
      updateWorkflowMessage(revision.workflowId, {
        phase: "Starting local renderer",
        progress: 15,
        detail: renderPlan.rationale,
      });
      const nextOutput = await executeRevision({
        baseOutputId: revision.outputId,
        instruction: revision.creatorRequest,
        summary: revision.creatorRequest.slice(0, 900),
        jobId,
        renderPlan,
      });
      delete revisionWorkflowByJobRef.current[jobId];
      setPreflight(null);
      setVideoPreviewActive(true);
      setSelectedOutputId(nextOutput.outputId);
      setActiveView("Projects");
      await refreshReviewRun(nextOutput.outputId);
      updateWorkflowMessage(revision.workflowId, {
        status: "completed",
        phase: `Draft v${nextOutput.version} ready`,
        progress: 100,
        detail: "The changed video passed local validation. Review it now, then choose Like or Dislike.",
        videoChanged: true,
        outputId: nextOutput.outputId,
      });
      setMessages((current) => [
        ...current,
        {
          id: `draft-feedback-${nextOutput.outputId}`,
          role: "reelbrain",
          sender: "ReelBrain",
          text: `Draft **v${nextOutput.version}** is ready and selected in Projects. The original draft was not overwritten.`,
          time: nowLabel(),
          draftFeedback: { outputId: nextOutput.outputId, version: nextOutput.version },
        },
      ]);
    } catch (error) {
      updateWorkflowMessage(revision.workflowId, {
        status: "failed",
        phase: "Approval failed",
        progress: 100,
        detail: `ReelBrain could not record or start this revision: ${String(error)}`,
        videoChanged: false,
      });
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not approve the revision: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      setRevisionBusy(false);
    }
  }

  function toggleDislike(target: string, outputId: string) {
    setDislikeTarget((current) => current === target ? null : target);
    setDislikeReasons((current) => ({
      ...current,
      [outputId]: current[outputId] ?? { wrong: "", change: "", preserve: "" },
    }));
  }

  async function likeDraft(output: ReviewOutput) {
    if (feedbackBusy || output.feedbackStatus !== "pending") return;
    setFeedbackBusy(output.outputId);
    try {
      const feedback = await recordRevisionFeedback({
        outputId: output.outputId,
        decision: "like",
        creatorStatement: `I like draft ${output.outputId} v${output.version}; remember the approved editing behavior with this draft as evidence.`,
      });
      await refreshReviewRun(output.outputId);
      await mutateMemory({
        action: "remember",
        category: `Approved ${output.mode} edit`,
        value: output.revisionSummary || output.rationale,
        scope: { output_mode: output.mode },
        creator_statement: `Remember the editing behavior approved by creator Like on ${output.outputId}; provenance event ${feedback.eventId}.`,
        source_evidence_event_id: feedback.eventId,
      });
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "reelbrain",
          sender: "ReelBrain",
          text: `You liked **v${output.version}**. I saved that choice as creator-controlled taste with provenance **${feedback.eventId}**. This version has left Review and remains in project history.`,
          time: nowLabel(),
        },
      ]);
    } catch (error) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "system", text: `Could not save draft feedback: ${String(error)}`, time: nowLabel() }]);
    } finally {
      setFeedbackBusy(null);
    }
  }

  async function skipDraft(output: ReviewOutput) {
    if (feedbackBusy || output.feedbackStatus !== "pending") return;
    setFeedbackBusy(output.outputId);
    try {
      const feedback = await recordRevisionFeedback({
        outputId: output.outputId,
        decision: "skip",
        creatorStatement: `Skip taste feedback for draft ${output.outputId} v${output.version}; keep the version in history without learning from it.`,
      });
      setDislikeTarget(null);
      await refreshReviewRun(output.outputId);
      await mutateMemory({
        action: "episode",
        category: `Inferred ${output.mode} request`,
        value: output.revisionSummary || output.rationale,
        scope: { output_mode: output.mode },
        creator_statement: `Record a tentative taste episode inferred from the request behind skipped draft ${output.outputId}; provenance event ${feedback.eventId}. Do not activate it without sufficient consistent examples and creator confirmation.`,
        source_evidence_event_id: feedback.eventId,
      });
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "reelbrain",
          sender: "ReelBrain",
          text: `Skipped direct feedback for **v${output.version}**. I saved your original request as a **tentative taste episode** linked to **${feedback.eventId}**. It will not affect edits as active taste unless enough consistent examples support it and you confirm it.`,
          time: nowLabel(),
        },
      ]);
    } catch (error) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "system", text: `Could not skip draft feedback: ${String(error)}`, time: nowLabel() }]);
    } finally {
      setFeedbackBusy(null);
    }
  }

  async function submitDislike(output: ReviewOutput) {
    if (feedbackBusy || output.feedbackStatus !== "pending") return;
    const answers = dislikeReasons[output.outputId] ?? { wrong: "", change: "", preserve: "" };
    if (answers.wrong.trim().length <= 2 || answers.change.trim().length <= 2) return;
    const reason = [
      `What felt wrong: ${answers.wrong.trim()}`,
      `Next draft must: ${answers.change.trim()}`,
      answers.preserve.trim() ? `Preserve: ${answers.preserve.trim()}` : null,
    ].filter(Boolean).join("\n");
    setFeedbackBusy(output.outputId);
    const workflowId = `feedback-revision-${crypto.randomUUID()}`;
    const jobId = `revision_${crypto.randomUUID().replaceAll("-", "_")}`;
    try {
      const feedback = await recordRevisionFeedback({
        outputId: output.outputId,
        decision: "dislike",
        reason,
        creatorStatement: `I dislike draft ${output.outputId} v${output.version}. Use my written correction to create the next draft.`,
      });
      setDislikeTarget(null);
      await refreshReviewRun(output.outputId);
      await mutateMemory({
        action: "remember",
        category: `Creator correction for ${output.mode}`,
        value: reason,
        scope: { output_mode: output.mode },
        creator_statement: `Remember the explicit correction submitted for ${output.outputId}; provenance event ${feedback.eventId}.`,
        source_evidence_event_id: feedback.eventId,
      });
      setMessages((current) => [
        ...current,
        {
          id: `workflow-${workflowId}`,
          role: "reelbrain",
          sender: "ReelBrain",
          text: `I recorded why v${output.version} missed your taste. That version has left Review; I’m applying your correction to v${output.version + 1}.`,
          time: nowLabel(),
          workflow: {
            id: workflowId,
            title: `Creating draft v${output.version + 1}`,
            phase: "Preparing correction",
            status: "running",
            progress: 5,
            detail: "Your reason is now durable taste evidence. The next render will not overwrite any previous version.",
            videoChanged: false,
            outputId: output.outputId,
          },
        },
      ]);
      revisionWorkflowByJobRef.current[jobId] = workflowId;
      updateWorkflowMessage(workflowId, {
        phase: "Planning from your correction",
        progress: 8,
        detail: "The Style Editor is translating your written reason into bounded render parameters without keyword rules.",
      });
      const renderPlan = await planRevision({
        instruction: reason,
        mode: output.mode,
        durationSeconds: output.durationSeconds,
        title: output.title,
        rationale: output.rationale,
        requestId: jobId,
      });
      if (!renderPlan.supported) throw new Error(renderPlan.unsupportedReason || "Your correction needs a tool capability the current renderer does not have.");
      updateWorkflowMessage(workflowId, {
        phase: "Starting corrected render",
        progress: 15,
        detail: renderPlan.rationale,
      });
      const nextOutput = await executeRevision({
        baseOutputId: output.outputId,
        instruction: reason,
        summary: answers.change.trim().slice(0, 900),
        jobId,
        renderPlan,
      });
      delete revisionWorkflowByJobRef.current[jobId];
      setPreflight(null);
      setVideoPreviewActive(true);
      setSelectedOutputId(nextOutput.outputId);
      setActiveView("Projects");
      await refreshReviewRun(nextOutput.outputId);
      setMessages((current) => [
        ...current,
        {
          id: `draft-feedback-${nextOutput.outputId}`,
          role: "reelbrain",
          sender: "ReelBrain",
          text: `Your correction is rendered in **v${nextOutput.version}**. Review the selected video and tell me whether this one feels right.`,
          time: nowLabel(),
          draftFeedback: { outputId: nextOutput.outputId, version: nextOutput.version },
        },
      ]);
    } catch (error) {
      delete revisionWorkflowByJobRef.current[jobId];
      updateWorkflowMessage(workflowId, {
        status: "failed",
        phase: "Next draft failed",
        progress: 100,
        detail: String(error),
        videoChanged: false,
      });
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "system", text: `Could not create the next draft: ${String(error)}`, time: nowLabel() }]);
    } finally {
      setFeedbackBusy(null);
    }
  }

  async function handleToolApproval(request: ToolApprovalRequest, action: "approve" | "deny" | "deploy") {
    if (toolApprovalBusy) return;
    setToolApprovalBusy(request.approvalId);
    try {
      const updateMessage = (updated: ToolApprovalRequest) => setMessages((current) => current.map((message) => (
        message.approvalRequest?.approvalId === updated.approvalId ? { ...message, approvalRequest: updated } : message
      )));
      if (action === "deploy" || (action === "deny" && request.status === "quarantined_pending_deploy_approval")) {
        const deployed = await deployTestedTool({
          approvalId: request.approvalId,
          decision: action === "deploy" ? "approve" : "deny",
          creatorStatement: action === "deploy"
            ? `I approve deploying the independently tested ${request.toolName} artifact with digest ${request.artifactDigest}.`
            : `I do not approve deploying ${request.toolName}. Keep the tested artifact quarantined and inactive.`,
        });
        updateMessage(deployed);
      } else {
        const creatorStatement = action === "approve"
          ? `I approve building ${request.toolName} in quarantine and running its bounded independent tests. I do not approve deployment or creator-data execution.`
          : `I do not approve creating, installing, deploying, or executing ${request.toolName}.`;
        const decided = await decideToolApproval({ approvalId: request.approvalId, decision: action, creatorStatement });
        updateMessage(decided);
        if (action === "approve") updateMessage(await buildAndTestTool(request.approvalId));
      }
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not record the tool approval decision: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      setToolApprovalBusy(null);
    }
  }

  function insertMention(value: string) {
    const mention = `@${value} `;
    setPrompt((current) => /^@[^\s]+\s*/u.test(current) ? current.replace(/^@[^\s]+\s*/u, mention) : `${mention}${current}`);
    window.setTimeout(() => promptRef.current?.focus(), 0);
  }

  function chooseMention(value: string) {
    setPrompt(`@${value} `);
    setMentionMenuDismissed(false);
    window.setTimeout(() => promptRef.current?.focus(), 0);
  }

  function chooseMentionSuggestion(suggestion: (typeof mentionSuggestions)[number]) {
    if (suggestion.kind === "timestamp") {
      addTimestampMention(videoPlayhead);
      setPrompt("");
      return;
    }
    chooseMention(suggestion.value);
  }

  function handlePromptKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (mentionMenuOpen) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        setMentionSelection((current) => (current + direction + mentionSuggestions.length) % mentionSuggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        chooseMentionSuggestion(mentionSuggestions[mentionSelection] ?? mentionSuggestions[0]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setMentionMenuDismissed(true);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSend();
    }
  }

  async function handleChatPaste(event: ReactClipboardEvent<HTMLTextAreaElement>) {
    const imageFiles = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (!imageFiles.length) return;
    event.preventDefault();
    const availableSlots = Math.max(0, 4 - chatAttachments.length);
    if (!availableSlots) return;
    setAttachmentBusy(true);
    try {
      for (const file of imageFiles.slice(0, availableSlots)) {
        const bytes = Array.from(new Uint8Array(await file.arrayBuffer()));
        const path = await persistChatImage({
          name: file.name || `clipboard-${Date.now()}.png`,
          mimeType: file.type || "image/png",
          bytes,
        });
        await attachChatImagePath(path);
      }
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not attach pasted image: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      setAttachmentBusy(false);
      window.setTimeout(() => promptRef.current?.focus(), 0);
    }
  }

  async function saveAgentProfile() {
    if (!agentEditorDraft || !agentProfiles || agentSaving) return;
    setAgentSaving(true);
    try {
      const next = await updateAgentProfile({
        expectedRevision: agentProfiles.revision,
        id: agentEditorDraft.id,
        name: agentEditorDraft.name,
        role: agentEditorDraft.role,
        systemPrompt: agentEditorDraft.systemPrompt,
      });
      setAgentProfiles(next);
      setAgentEditorDraft(null);
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Saved editing persona “${agentEditorDraft.name}” at revision ${next.revision}.`, time: nowLabel() },
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not save editing persona: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      setAgentSaving(false);
    }
  }

  async function startTeam(steering = prompt.trim(), existingWorkflowId?: string) {
    if (!preflight || preflight.status !== "ready" || fanoutBusy) return;
    if (!connection.connected) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: "Connect Codex before starting the editorial agents.", time: nowLabel() },
      ]);
      return;
    }
    const workflowId = existingWorkflowId || `fanout-${crypto.randomUUID()}`;
    if (existingWorkflowId) {
      updateWorkflowMessage(workflowId, {
        title: "Preparing a governed edit plan",
        phase: "Starting agent team",
        status: "running",
        progress: 8,
        detail: "Approval is recorded. ReelBrain is preparing bounded context for four independent editors.",
        videoChanged: false,
      });
    } else {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "reelbrain",
          sender: "ReelBrain",
          text: "",
          time: nowLabel(),
          workflow: {
            id: workflowId,
            title: "Preparing a governed edit plan",
            phase: "Starting agent team",
            status: "running",
            progress: 8,
            detail: "ReelBrain is preparing bounded context for four independent editors.",
            videoChanged: false,
          },
        },
      ]);
    }
    fanoutWorkflowIdRef.current = workflowId;
    fanoutAgentStatusesRef.current = {};
    setFanoutBusy(true);
    setAgentStates(Object.fromEntries(AGENTS.map((agent) => [agent.id, { status: "authorizing", detail: "Waiting for a capability packet" }])));
    try {
      const result = await startEditorialFanout({
        sourcePath: preflight.path,
        sourceSha256: preflight.sha256,
        projectId: "founder-desktop-project",
        creatorId: "creator-founder",
        currentSteering: steering || null,
      });
      setFanout(result);
      if (result.status === "TRANSCRIPT_REQUIRED") {
        setAgentStates(INITIAL_AGENT_STATES);
        updateWorkflowMessage(workflowId, {
          status: "blocked",
          phase: "Transcription approval required",
          progress: 100,
          detail: result.message || "ReelBrain cannot begin grounded editing until a transcript is authorized.",
          videoChanged: false,
        });
        setMessages((current) => [
          ...current,
          { id: crypto.randomUUID(), role: "system", text: result.message || "Transcription approval is required before fan-out.", time: nowLabel() },
        ]);
      } else {
        updateWorkflowMessage(workflowId, {
          status: "completed",
          phase: "Grounded plan complete",
          progress: 100,
          detail: "Four editors completed, the Showrunner validated their grounded candidate IDs, and an accepted plan was stored. Rendering has not started.",
          videoChanged: false,
        });
        setMessages((current) => [
          ...current,
          {
            id: crypto.randomUUID(),
            role: "reelbrain",
            sender: "ReelBrain",
            text: `Agent work is complete. ReelBrain accepted only grounded candidate IDs and stored plan \`${result.planDigest}\`. **The video is still unchanged because rendering has not started.**`,
            time: nowLabel(),
          },
        ]);
      }
      await refreshDurableState();
    } catch (error) {
      updateWorkflowMessage(workflowId, {
        status: "failed",
        phase: "Agent work failed",
        progress: 100,
        detail: String(error),
        videoChanged: false,
      });
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Editorial fan-out failed: ${String(error)}`, time: nowLabel() },
      ]);
    } finally {
      fanoutWorkflowIdRef.current = null;
      fanoutAgentStatusesRef.current = {};
      setFanoutBusy(false);
    }
  }

  async function steerFanout(message: string, action: "steer" | "cancel" = "steer") {
    if (!fanout?.fanoutId || !fanout.rootAuthorityToken) {
      await handleSend(message);
      return;
    }
    try {
      await steerEditorialFanout({
        fanout_id: fanout.fanoutId,
        root_capability_token: fanout.rootAuthorityToken,
        action,
        message,
      });
      setAgentStates((current) =>
        Object.fromEntries(Object.entries(current).map(([key, value]) => [key, { ...value, status: "stale", detail: "Revoked by creator steering; re-plan required" }])),
      );
      setFanout((current) => (current ? { ...current, status: action === "cancel" ? "CANCELLED" : "REQUIRES_REPLAN" } : current));
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `${action === "cancel" ? "Cancelled" : "Steered"} the governed fan-out. Previous grants are revoked.`, time: nowLabel() },
      ]);
      await refreshDurableState();
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Could not steer fan-out: ${String(error)}`, time: nowLabel() },
      ]);
    }
  }

  async function mutateMemory(input: Record<string, unknown>): Promise<boolean> {
    if (!memory || memoryBusy) return false;
    setMemoryBusy(true);
    try {
      const next = await mutateCreatorMemory({
        creator_id: "creator-founder",
        expected_revision: memory.revision,
        project_id: "founder-desktop-project",
        ...input,
      });
      setMemory(next);
      await refreshDurableState();
      return true;
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: `Taste update failed: ${String(error)}`, time: nowLabel() },
      ]);
      return false;
    } finally {
      setMemoryBusy(false);
    }
  }

  async function rememberPrompt() {
    const value = prompt.trim();
    if (!value) {
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "system", text: "Type the preference you want ReelBrain to remember first.", time: nowLabel() },
      ]);
      return;
    }
    await mutateMemory({
      action: "remember",
      category: "Explicit instruction",
      value,
      scope: {},
      creator_statement: `Remember this explicit preference: ${value}`,
    });
    setPrompt("");
  }

  function editPreference(preference: TastePreference) {
    setTasteEditorDraft({ ...preference, scope: { ...preference.scope } });
  }

  async function saveTastePreference() {
    if (!tasteEditorDraft || memoryBusy) return;
    const value = tasteEditorDraft.value.trim();
    if (!value) return;
    const saved = await mutateMemory({
      action: "edit",
      preference_id: tasteEditorDraft.id,
      value,
      scope: {
        output_mode: tasteEditorDraft.scope.outputMode?.trim() || null,
        content_kind: tasteEditorDraft.scope.contentKind?.trim() || null,
        language: tasteEditorDraft.scope.language?.trim() || null,
      },
      creator_statement: `Correct preference ${tasteEditorDraft.id} to: ${value}`,
    });
    if (saved) setTasteEditorDraft(null);
  }

  function forgetPreference(preference: TastePreference) {
    setTasteForgetTarget(preference);
  }

  async function confirmForgetPreference() {
    if (!tasteForgetTarget || memoryBusy) return;
    const forgotten = await mutateMemory({
      action: "delete",
      preference_id: tasteForgetTarget.id,
      creator_statement: `Permanently forget preference ${tasteForgetTarget.id}`,
    });
    if (forgotten) setTasteForgetTarget(null);
  }

  async function reviewAction(action: "approve" | "reject" | "revise", output = selectedOutput) {
    if (!output) return;
    const statements = {
      approve: `Approve ${output.title} as a creator-review draft; do not publish.`,
      reject: `Reject ${output.title} for this review round.`,
      revise: `Request a revision of ${output.title} while preserving source grounding.`,
    };
    await recordReviewAction({
      action,
      output_id: output.outputId,
      creator_statement: statements[action],
      creator_id: "creator-founder",
      project_id: "founder-desktop-project",
    });
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "system", text: `${statements[action]} State remains CREATOR_REVIEW and publish-ready remains false.`, time: nowLabel() },
    ]);
    await refreshDurableState();
  }

  function renderVideo(preferSource = false) {
    if (preferSource && preflight) {
      return <LocalVideo path={preflight.path} onPlayheadChange={handleVideoPlayheadChange} onAddTimestamp={addTimestampMention} />;
    }
    if (selectedOutput?.video && videoPreviewActive) {
      return (
        <LocalVideo
          path={selectedOutput.video}
          poster={selectedOutput.thumbnail}
          autoPlay
          onPlayheadChange={handleVideoPlayheadChange}
          onAddTimestamp={addTimestampMention}
        />
      );
    }
    if (selectedOutput?.thumbnail) {
      return (
        <button
          className="video-poster"
          onClick={() => setVideoPreviewActive(true)}
          aria-label={`Play ${selectedOutput.title}`}
        >
          <i className="video-poster-backdrop" style={{ backgroundImage: `url(${pathToUrl(selectedOutput.thumbnail)})` }} aria-hidden="true" />
          <i className="video-poster-media" style={{ backgroundImage: `url(${pathToUrl(selectedOutput.thumbnail)})` }} aria-hidden="true" />
          <span><Play size={30} fill="currentColor" /></span>
        </button>
      );
    }
    return (
      <div className="video-empty">
        <Film size={44} />
        <strong>Drop a creator-owned video to begin</strong>
        <span>ReelBrain performs local preflight before any provider effect.</span>
      </div>
    );
  }

  function SourceDrop({ compact = false }: { compact?: boolean }) {
    return (
      <button className={`source-drop ${compact ? "source-drop--compact" : ""}`} onClick={() => void chooseVideo()}>
        {dropBusy ? <LoaderCircle className="spin" /> : <UploadCloud />}
        <strong>{preflight ? preflight.name : "Drop a video or choose a local file"}</strong>
        <span>
          {preflight
            ? `${preflight.message} · ${preflight.width ?? "?"}×${preflight.height ?? "?"} · ${preflight.durationSeconds ? formatDuration(preflight.durationSeconds) : "unknown duration"}`
            : "MP4, MOV, M4V, MKV, or WebM · local FFprobe and SHA-256 first"}
        </span>
      </button>
    );
  }

  function HomeView() {
    const projects = Array.from(
      run.outputs.reduce((groups, output) => {
        const sourceId = output.outputId.match(/^source-\d+/)?.[0] || "project";
        const current = groups.get(sourceId) || [];
        current.push(output);
        groups.set(sourceId, current);
        return groups;
      }, new Map<string, ReviewOutput[]>()),
    ).map(([sourceId, outputs]) => {
      const lead = outputs.find((output) => output.mode === "long") || outputs[0];
      return {
        sourceId,
        outputs,
        lead,
        shorts: outputs.filter((output) => output.mode === "short").length,
        longForms: outputs.filter((output) => output.mode === "long").length,
      };
    });

    return (
      <section className="page-view home-view">
        <div className="page-hero">
          <span className="eyebrow"><Sparkles size={14} /> Your AI editing room</span>
          <h1>Your video. Your taste. A team that learns both.</h1>
          <p>Four editing personas shape the story, pace, style, and continuity—then improve from every correction you choose to remember.</p>
        </div>
        <SourceDrop />
        <section className="home-section editing-team-section">
          <div className="home-section-heading">
            <div><span>Editing team</span><h2>Four perspectives. One coherent cut.</h2></div>
            <small>Independent Codex threads · grounded candidates only</small>
          </div>
          <div className="persona-grid">
            {configuredAgents.map((agent, index) => {
              const Icon = agent.icon;
              return (
                <article className={`persona-card persona-card--${agent.color}`} key={agent.id}>
                  <header><span><Icon size={17} /></span><em>0{index + 1}</em></header>
                  <h3>{agent.profile.name}</h3>
                  <p>{agent.profile.role}</p>
                  <small>{agent.focus}</small>
                  <div className="persona-tools">
                    <span><Wrench size={11} /> Assigned tools</span>
                    <div>{agent.tools.map((tool) => <code key={tool}>{tool}</code>)}</div>
                  </div>
                  <button className="persona-edit" disabled={!agentProfiles} onClick={() => setAgentEditorDraft({ ...agent.profile })}><Edit3 size={13} /> Edit persona</button>
                </article>
              );
            })}
          </div>
        </section>

        <section className="home-section project-library-section">
          <div className="home-section-heading">
            <div><span>Projects</span><h2>Continue where you left off.</h2></div>
            <small>{run.available ? `${projects.length} projects · ${run.outputs.length} drafts` : "No projects yet"}</small>
          </div>
          {projects.length ? (
            <div className="project-library">
              {projects.map((project, index) => (
                <button
                  className="project-row"
                  key={project.sourceId}
                  onClick={() => {
                    setSelectedOutputId(project.lead.outputId);
                    setActiveView("Projects");
                  }}
                >
                  <span className="project-row-thumbnail" style={{ backgroundImage: `url(${pathToUrl(project.lead.thumbnail)})` }} />
                  <span className="project-row-copy">
                    <em>Project {String(index + 1).padStart(2, "0")}</em>
                    <strong>{project.lead.title}</strong>
                    <small>{project.shorts} Shorts · {project.longForms} long-form · {run.status}</small>
                  </span>
                  <ArrowRight size={17} />
                </button>
              ))}
            </div>
          ) : (
            <div className="honest-empty"><FolderKanban /><strong>No projects yet.</strong><span>Choose a local source above to begin your first edit.</span></div>
          )}
        </section>
      </section>
    );
  }

  function ProjectsView() {
    const previewPortrait = preflight
      ? Number(preflight.height || 0) > Number(preflight.width || 0)
      : selectedOutput?.mode === "short";
    return (
      <section
        className={`workspace project-workspace ${chatResizing ? "is-chat-resizing" : ""}`}
        style={{ "--chat-width": `${chatWidth}px` } as React.CSSProperties}
      >
        <section className="editor-panel">
          <div className={`video-frame ${previewPortrait ? "video-frame--portrait" : "video-frame--landscape"}`}>
            {renderVideo(Boolean(preflight))}
            <div className="video-title-chip">
              <Film size={13} />
              <span>{preflight ? preflight.name : selectedOutput?.title || "No video selected"}</span>
              <em>{preflight ? "Local source" : selectedOutput ? formatDuration(selectedOutput.durationSeconds) : ""}</em>
            </div>
          </div>
          {!preflight && selectedVersionFamily.length > 1 && (
            <nav className="version-strip" aria-label="Draft version history">
              <span>Version history</span>
              <div>
                {selectedVersionFamily.map((output) => (
                  <button
                    key={output.outputId}
                    className={selectedOutput?.outputId === output.outputId ? "is-current" : ""}
                    onClick={() => {
                      setSelectedOutputId(output.outputId);
                      setVideoPreviewActive(false);
                    }}
                  >
                    v{output.version}
                    {output.feedbackStatus === "liked" ? <ThumbsUp size={10} /> : output.feedbackStatus === "disliked" ? <ThumbsDown size={10} /> : output.feedbackStatus === "skipped" ? <SkipForward size={10} /> : output.isRevision ? <i /> : null}
                  </button>
                ))}
              </div>
              <small>Previous versions stay available even after feedback.</small>
            </nav>
          )}
          <div className="editor-toolbar">
            <SourceDrop compact />
            <div className="project-action-row">
              <div>
                <strong>{preflight ? "Ready for a new edit" : selectedOutput ? "Creator-review draft" : "Choose a source"}</strong>
                <span>{fanoutBusy ? "Four agents are working" : fanout?.status || run.status}</span>
              </div>
              <button className="primary-action" disabled={!preflight || preflight.status !== "ready" || fanoutBusy} onClick={() => void startTeam()}>
                {fanoutBusy ? <LoaderCircle className="spin" size={16} /> : <Bot size={16} />}
                {fanoutBusy ? "Working…" : "New edit"}
              </button>
            </div>
            {!preflight && selectedOutput?.isRevision && (
              <DraftFeedbackCard
                output={selectedOutput}
                busy={feedbackBusy === selectedOutput.outputId}
                expanded={dislikeTarget === `projects:${selectedOutput.outputId}`}
                reason={dislikeReasons[selectedOutput.outputId] ?? { wrong: "", change: "", preserve: "" }}
                onReasonChange={(reason) => setDislikeReasons((current) => ({ ...current, [selectedOutput.outputId]: reason }))}
                onLike={() => void likeDraft(selectedOutput)}
                onSkip={() => void skipDraft(selectedOutput)}
                onDislike={() => toggleDislike(`projects:${selectedOutput.outputId}`, selectedOutput.outputId)}
                onSubmitDislike={() => void submitDislike(selectedOutput)}
              />
            )}
          </div>
        </section>

        <aside className={`chat-panel ${chatDropActive ? "is-image-drop-target" : ""}`}>
          <div
            className={`chat-resize-handle ${chatResizing ? "is-active" : ""}`}
            role="separator"
            tabIndex={0}
            aria-label="Resize chat panel"
            aria-orientation="vertical"
            aria-valuemin={272}
            aria-valuemax={544}
            aria-valuenow={Math.round(chatWidth)}
            title="Drag to resize chat · Double-click to reset"
            onPointerDown={handleChatResizeStart}
            onPointerMove={handleChatResizeMove}
            onPointerUp={handleChatResizeEnd}
            onPointerCancel={handleChatResizeEnd}
            onDoubleClick={() => setChatWidth(320)}
            onKeyDown={handleChatResizeKey}
          ><span /></div>
          {chatDropActive && <div className="chat-drop-overlay"><ImagePlus size={22} /><strong>Attach images to this chat</strong><span>Drop up to four PNG, JPEG, WebP, or GIF files.</span></div>}
          <div className="panel-heading">
            <h2>Chat with ReelBrain <span className="root-thread-badge">Root Codex thread</span></h2>
            <button className="small-icon" title="Refresh connection" onClick={() => void refreshConnection()}><RefreshCw size={15} /></button>
          </div>
          <div
            className="message-list"
            ref={messageListRef}
            onScroll={(event) => {
              const list = event.currentTarget;
              const distanceFromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
              chatScrollRef.current = { top: list.scrollTop, followBottom: distanceFromBottom < 48 };
            }}
          >
            {messages.map((message) => (
              <article className={`message message--${message.role}`} key={message.id}>
                <header>
                  {message.role === "creator" ? <MessageSquareText size={17} /> : message.role === "approval" ? <Wrench size={15} /> : <BrainMark compact />}
                  <strong>{message.role === "creator" ? "You" : message.sender || (message.role === "system" ? "System" : "ReelBrain")}</strong>
                  <time>{message.time}</time>
                </header>
                {message.text && <RichText>{message.text}</RichText>}
                {message.attachments?.length ? (
                  <div className="message-attachments">
                    {message.attachments.map((attachment) => <img key={attachment.id} src={attachment.previewUrl ?? pathToUrl(attachment.path)} alt={attachment.name} />)}
                  </div>
                ) : null}
                {message.approvalRequest && (
                  <ToolApprovalCard
                    request={message.approvalRequest}
                    busy={toolApprovalBusy === message.approvalRequest.approvalId}
                    onAction={(action) => void handleToolApproval(message.approvalRequest!, action)}
                  />
                )}
                {message.workflow && (
                  <WorkflowProgressCard
                    workflow={message.workflow}
                    busy={revisionBusy}
                    onApprove={message.workflow.status === "awaiting_approval" && pendingRevision?.workflowId === message.workflow.id ? () => void approvePendingRevision(pendingRevision) : undefined}
                    onDismiss={message.workflow.status === "awaiting_approval" && pendingRevision?.workflowId === message.workflow.id ? () => dismissPendingRevision(message.workflow!.id) : undefined}
                  />
                )}
                {message.draftFeedback && (() => {
                  const feedbackOutput = run.outputs.find((output) => output.outputId === message.draftFeedback?.outputId);
                  if (!feedbackOutput) return null;
                  return (
                    <DraftFeedbackCard
                      output={feedbackOutput}
                      busy={feedbackBusy === feedbackOutput.outputId}
                      expanded={dislikeTarget === `chat:${message.id}:${feedbackOutput.outputId}`}
                      reason={dislikeReasons[feedbackOutput.outputId] ?? { wrong: "", change: "", preserve: "" }}
                      onReasonChange={(reason) => setDislikeReasons((current) => ({ ...current, [feedbackOutput.outputId]: reason }))}
                      onLike={() => void likeDraft(feedbackOutput)}
                      onSkip={() => void skipDraft(feedbackOutput)}
                      onDislike={() => toggleDislike(`chat:${message.id}:${feedbackOutput.outputId}`, feedbackOutput.outputId)}
                      onSubmitDislike={() => void submitDislike(feedbackOutput)}
                      onView={() => {
                        setPreflight(null);
                        setSelectedOutputId(feedbackOutput.outputId);
                        setVideoPreviewActive(true);
                        setActiveView("Projects");
                      }}
                    />
                  );
                })()}
                <ActivityFeed activities={message.activities || []} />
              </article>
            ))}
            {chatBusy && <article className="message thinking-message" key="thinking"><p><LoaderCircle size={15} className="spin" /> {chatActivity || "ReelBrain is reasoning…"}</p>{activeWorkflow && <WorkflowProgressCard workflow={activeWorkflow} />}<ActivityFeed activities={chatActivities} live /></article>}
          </div>
          <div className="mention-bar" aria-label="Mention an editing agent">
            <button onClick={() => insertMention("team")}><Bot size={12} /> @Team</button>
            {configuredAgents.map((agent) => (
              <button key={agent.id} title={`Mention ${agent.profile.name}`} onClick={() => insertMention(mentionSlug(agent.profile.name))}>
                @{mentionSlug(agent.profile.name)}
              </button>
            ))}
          </div>
          {chatAttachments.length > 0 && (
            <div className="attachment-strip" aria-label="Attached images">
              {chatAttachments.map((attachment) => (
                <div className="attachment-chip" key={attachment.id}>
                  <img src={attachment.previewUrl ?? pathToUrl(attachment.path)} alt="" />
                  <span>{attachment.name}</span>
                  <button onClick={() => setChatAttachments((current) => current.filter((item) => item.id !== attachment.id))} aria-label={`Remove ${attachment.name}`}><X size={12} /></button>
                </div>
              ))}
              <small>{chatAttachments.length}/4 · sent with this turn</small>
            </div>
          )}
          <div className="composer-wrap">
            {mentionMenuOpen && (
              <div className="mention-suggestions" role="listbox" aria-label="Mention an editing agent or timestamp">
                <div className="mention-suggestions__label">Mention an agent or moment</div>
                {mentionSuggestions.map((suggestion, index) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === mentionSelection}
                    className={index === mentionSelection ? "is-active" : ""}
                    key={`${suggestion.kind}:${suggestion.value}`}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setMentionSelection(index)}
                    onClick={() => chooseMentionSuggestion(suggestion)}
                  >
                    <span className="mention-avatar">{suggestion.kind === "timestamp" ? <Clock3 size={14} /> : suggestion.value === "team" ? <Bot size={14} /> : suggestion.label.slice(0, 1).toLocaleUpperCase()}</span>
                    <span>
                      <strong>{suggestion.kind === "timestamp" ? suggestion.label : `@${suggestion.value}`}</strong>
                      <small>{suggestion.kind === "timestamp" ? `Add @${suggestion.value} to chat · ${suggestion.detail}` : `${suggestion.label} · ${suggestion.detail}`}</small>
                    </span>
                    {index === mentionSelection && <kbd>{mentionSuggestions.length === 1 ? "Enter" : "↵"}</kbd>}
                  </button>
                ))}
              </div>
            )}
            <div className="composer">
              <div className="composer-input">
                {timestampMentions.length > 0 && (
                  <div className="composer-timestamps" aria-label="Mentioned video timestamps">
                    {timestampMentions.map((seconds) => (
                      <span className="timestamp-chip" key={seconds}>
                        <Clock3 size={11} /><strong>@{formatMediaTime(seconds)}</strong>
                        <button type="button" aria-label={`Remove ${formatMediaTime(seconds)}`} onClick={() => setTimestampMentions((current) => current.filter((item) => item !== seconds))}><X size={10} /></button>
                      </span>
                    ))}
                  </div>
                )}
                <textarea
                  ref={promptRef}
                  value={prompt}
                  onPaste={(event) => void handleChatPaste(event)}
                  onChange={(event) => {
                    setPrompt(event.target.value);
                    setMentionMenuDismissed(false);
                  }}
                  onKeyDown={handlePromptKeyDown}
                  placeholder="Ask, ⌘V an image, or drop one here…"
                />
              </div>
              <button onClick={() => void handleSend()} disabled={chatBusy || attachmentBusy} aria-label="Send message">{chatBusy || attachmentBusy ? <LoaderCircle size={18} className="spin" /> : <Send size={18} />}</button>
            </div>
          </div>
          <div className="steering-actions">
            <button onClick={() => void steerFanout("Preserve the complete context and caveats.")}><ShieldCheck size={15} /> Preserve context</button>
            <button onClick={() => void steerFanout("Make the edit tighter without cutting mid-thought.")}><Scissors size={15} /> Make it tighter</button>
            <button onClick={() => void rememberPrompt()} disabled={memoryBusy}><Heart size={15} /> Remember typed taste</button>
          </div>
          {fanout?.fanoutId && fanout.status !== "REQUIRES_REPLAN" && fanout.status !== "CANCELLED" && (
            <button className="cancel-fanout" onClick={() => void steerFanout("Creator stopped this editorial attempt.", "cancel")}><PauseCircle size={14} /> Cancel current fan-out</button>
          )}
        </aside>

        <section className="drafts-panel">
          <div className="section-title">
            <h2>Drafts for review <span>{run.outputs.length}</span></h2>
            <small>{run.available ? `${run.status} · existing governed run` : "No local drafts"}</small>
          </div>
          {run.outputs.length ? (
            <div className="drafts-grid">
              {run.outputs.filter((output) => output.mode === "short").slice(0, 3).map((output) => (
                <button className={`draft-card ${selectedOutput?.outputId === output.outputId ? "is-selected" : ""}`} key={output.outputId} onClick={() => setSelectedOutputId(output.outputId)}>
                  <div className="draft-thumb" style={{ backgroundImage: `url(${pathToUrl(output.thumbnail)})` }}><span className="mode-badge">Short</span><time>{formatDuration(output.durationSeconds)}</time></div>
                  <strong>{output.title}</strong>
                </button>
              ))}
              {run.outputs.find((output) => output.mode === "long") && (() => {
                const output = run.outputs.find((item) => item.mode === "long")!;
                return (
                  <button className={`draft-card draft-card--long ${selectedOutput?.outputId === output.outputId ? "is-selected" : ""}`} onClick={() => setSelectedOutputId(output.outputId)}>
                    <div className="long-thumb" style={{ backgroundImage: `url(${pathToUrl(output.thumbnail)})` }}><span className="mode-badge mode-badge--long">Long-form</span><time>{formatDuration(output.durationSeconds)}</time></div>
                    <strong>{output.title}</strong>
                  </button>
                );
              })()}
            </div>
          ) : <div className="honest-empty"><Film /><strong>No drafts exist for this project.</strong><span>Run transcription, grounded selection, validation, and rendering before review cards appear.</span></div>}
        </section>
      </section>
    );
  }

  function MemoryEvidenceView() {
    const events = [...evidence.events, ...evidence.reviewEvents.map((event) => ({
      eventType: event.eventType,
      actor: "creator",
      decision: "allow" as const,
      reasonCode: event.action,
      receiptId: event.eventId,
      createdAt: event.at,
      details: { outputId: event.outputId, resultingState: event.resultingState, publishReady: event.publishReady },
    }))].sort((a, b) => (evidenceDate(b.createdAt)?.getTime() || 0) - (evidenceDate(a.createdAt)?.getTime() || 0));
    const preferences = memory?.preferences ?? [];
    return (
      <section className="page-view taste-management memory-evidence-view">
        <div className="page-heading-row">
          <div><span className="eyebrow"><Heart size={14} /> Memory with provenance</span><h1>Memory & Evidence</h1><p>{memory?.principle} Every remembered preference remains linked to inspectable provenance and can be corrected or removed.</p></div>
          <button className="secondary-action" onClick={() => void refreshDurableState()}><RefreshCw size={15} /> Refresh</button>
        </div>
        <MemoryEvidenceGraph preferences={preferences} evidenceCount={events.length} />
        {memory?.proposals.length ? (
          <section className="proposal-section">
            <h2>Awaiting confirmation</h2>
            {memory.proposals.map((proposal) => (
              <article className="proposal-card" key={proposal.proposalId}>
                <div><Sparkles /><strong>{proposal.value}</strong><span>{proposal.category} · {scopeLabel(proposal.scope)} · {proposal.evidenceEventIds.length} examples</span></div>
                <button onClick={() => void mutateMemory({ action: "confirm", proposal_id: proposal.proposalId, creator_statement: `Confirm learned preference: ${proposal.value}` })}><Check size={15} /> Confirm</button>
              </article>
            ))}
          </section>
        ) : <div className="quiet-note"><Info size={15} /> No inferred preference is awaiting confirmation. ReelBrain needs at least two consistent episode examples.</div>}
        <div className="memory-evidence-layout">
          <section className="memory-preferences-column">
            <div className="memory-section-heading"><div><span>Behavior priors</span><h2>Creator-controlled taste</h2></div><small>Revision {memory?.revision ?? "…"}</small></div>
            <div className="preference-grid">
              {preferences.map((preference) => (
                <article className={`preference-card ${preference.status === "disabled" ? "is-disabled" : ""}`} key={preference.id}>
                  <header><span>{preference.category}</span><em>{preference.explicit ? "Explicit" : "Confirmed inference"}</em></header>
                  <h2>{preference.value}</h2>
                  <p>{scopeLabel(preference.scope)}</p>
                  <div className="preference-meta"><span>v{preference.version}</span><span>{Math.round(preference.confidence * 100)}% confidence</span><span>{preference.provenanceEventIds.length} provenance event(s)</span></div>
                  <div className="preference-actions">
                    <button disabled={memoryBusy} onClick={() => editPreference(preference)}><Edit3 size={14} /> Edit</button>
                    <button disabled={memoryBusy} onClick={() => void mutateMemory({ action: preference.status === "active" ? "disable" : "enable", preference_id: preference.id, creator_statement: `${preference.status === "active" ? "Disable" : "Enable"} preference ${preference.id}` })}>
                      {preference.status === "active" ? <ToggleRight size={15} /> : <ToggleLeft size={15} />} {preference.status === "active" ? "Disable" : "Enable"}
                    </button>
                    <button disabled={memoryBusy} className="danger-link" onClick={() => forgetPreference(preference)}><Trash2 size={14} /> Forget</button>
                  </div>
                </article>
              ))}
            </div>
            <div className="memory-privacy"><ShieldCheck /><div><strong>Deletion is content-removing and restart-safe.</strong><p>Forgotten values are removed. Only a content-free tombstone and deletion fence remain to prevent resurrection.</p></div></div>
          </section>
          <section className="memory-audit-column">
            <div className="memory-section-heading"><div><span>Trust trail</span><h2>Recent evidence</h2></div><small>{events.length} events</small></div>
            {events.length ? <div className="evidence-timeline">{events.slice(0, 18).map((event, index) => (
              <article key={event.receiptId || `${event.eventType}-${index}`}>
                <span className={`evidence-dot ${event.decision === "deny" ? "deny" : "allow"}`} />
                <div><header><strong>{friendlyEvent(event.eventType)}</strong><em>{event.decision || "recorded"}</em></header><p>{friendlyEvent(event.reasonCode)} · actor: {event.actor || "ReelBrain"}</p><small>{evidenceDateLabel(event.createdAt)} · {event.receiptId}</small></div>
              </article>
            ))}</div> : <div className="honest-empty"><FileCheck2 /><strong>No desktop evidence yet.</strong><span>Governed agent work and creator decisions will appear here.</span></div>}
            <div className="raw-evidence-actions"><button disabled={!run.manifestPath} onClick={() => void revealLocalEvidence(run.manifestPath)}><FolderKanban size={15} /> Open run manifest</button>{fanout?.planPath && <button onClick={() => void revealLocalEvidence(fanout.planPath)}><FileCheck2 size={15} /> Open accepted plan</button>}</div>
          </section>
        </div>
      </section>
    );
  }

  function ReviewView() {
    const reviewSelected = pendingReviewOutputs.find((output) => output.outputId === selectedOutputId) ?? pendingReviewOutputs[0] ?? null;
    return (
      <section className="page-view review-view">
        <div className="page-heading-row"><div><span className="eyebrow"><Eye size={14} /> Creator decision gate</span><h1>Review {pendingReviewOutputs.length} drafts</h1><p>Answered versions leave this queue but remain in project history. Nothing here publishes a video.</p></div><span className="review-state">{run.status}</span></div>
        {reviewSelected ? (
          <div className="review-layout">
            <div className="review-preview">
              <div className={`review-video ${reviewSelected.mode === "short" ? "review-video--portrait" : "review-video--landscape"}`}>
                {selectedOutput?.outputId === reviewSelected.outputId ? renderVideo() : (
                  <button className="video-poster" onClick={() => { setSelectedOutputId(reviewSelected.outputId); setVideoPreviewActive(true); }} aria-label={`Play ${reviewSelected.title}`}>
                    <i className="video-poster-backdrop" style={{ backgroundImage: `url(${pathToUrl(reviewSelected.thumbnail)})` }} aria-hidden="true" />
                    <i className="video-poster-media" style={{ backgroundImage: `url(${pathToUrl(reviewSelected.thumbnail)})` }} aria-hidden="true" />
                    <span><Play size={30} fill="currentColor" /></span>
                  </button>
                )}
              </div>
              <div className="review-summary"><span>{reviewSelected.mode} · v{reviewSelected.version}</span><h2>{reviewSelected.title}</h2><p>{reviewSelected.rationale}</p><div><em>0:00 – {formatMediaTime(reviewSelected.durationSeconds)}</em><em>{reviewSelected.captionAccuracyStatus}</em></div></div>
              {reviewSelected.isRevision ? (
                <div className="review-feedback-wrap">
                  <DraftFeedbackCard
                    output={reviewSelected}
                    busy={feedbackBusy === reviewSelected.outputId}
                    expanded={dislikeTarget === `review:${reviewSelected.outputId}`}
                    reason={dislikeReasons[reviewSelected.outputId] ?? { wrong: "", change: "", preserve: "" }}
                    onReasonChange={(reason) => setDislikeReasons((current) => ({ ...current, [reviewSelected.outputId]: reason }))}
                    onLike={() => void likeDraft(reviewSelected)}
                    onSkip={() => void skipDraft(reviewSelected)}
                    onDislike={() => toggleDislike(`review:${reviewSelected.outputId}`, reviewSelected.outputId)}
                    onSubmitDislike={() => void submitDislike(reviewSelected)}
                  />
                </div>
              ) : (
                <div className="review-actions">
                  <button className="approve" onClick={() => void reviewAction("approve", reviewSelected)}><CheckCircle2 size={16} /> Approve draft</button>
                  <button onClick={() => { setSelectedOutputId(reviewSelected.outputId); setActiveView("Projects"); setPrompt(`Revise ${reviewSelected.title}: `); window.setTimeout(() => promptRef.current?.focus(), 0); }}><RotateCcw size={16} /> Request revision</button>
                  <button className="reject" onClick={() => void reviewAction("reject", reviewSelected)}><X size={16} /> Reject</button>
                </div>
              )}
            </div>
            <div className="review-list">
              {pendingReviewOutputs.map((output) => (
                <button className={reviewSelected.outputId === output.outputId ? "is-selected" : ""} key={output.outputId} onClick={() => { setSelectedOutputId(output.outputId); setVideoPreviewActive(false); }}>
                  <div style={{ backgroundImage: `url(${pathToUrl(output.thumbnail)})` }} /><span><strong>{output.title}</strong><em>{output.mode} · v{output.version} · {formatDuration(output.durationSeconds)}</em></span>
                </button>
              ))}
            </div>
          </div>
        ) : <div className="honest-empty large"><CheckCircle2 /><strong>You’re caught up.</strong><span>All draft feedback is answered. Previous versions remain available in Projects.</span></div>}
      </section>
    );
  }

  function SettingsView() {
    return (
      <section className="page-view settings-view">
        <div className="page-heading-row"><div><span className="eyebrow"><Settings size={14} /> Local runtime</span><h1>Settings</h1><p>Exact account, tool, timeout, and data boundaries.</p></div></div>
        <div className="settings-grid">
          <article><header><BrainMark compact /><strong>Codex account</strong></header><h2>{runtimeLabel}</h2><p>{connection.detail}</p><button onClick={connection.connected ? () => void refreshConnection() : () => void handleConnect()}>{connection.connected ? "Refresh account state" : "Connect through Codex"}</button></article>
          <article><header><Activity size={18} /><strong>Local dependencies</strong></header><div className="health-list large">{(["codex", "python", "ffmpeg", "ffprobe"] as const).map((key) => <span key={key}><i className={health?.[key] ? "ok" : "bad"} /> {key}<em>{health?.[key] ? "Ready" : "Missing"}</em></span>)}</div></article>
          <article><header><Clock3 size={18} /><strong>Bounded execution</strong></header><ul><li>Root chat: {health?.chatTimeoutSeconds ?? 90}s</li><li>Persona thread: {health?.agentTimeoutSeconds ?? 120}s</li><li>Local bridge: {health?.bridgeTimeoutSeconds ?? 20}s</li></ul><p>Timed-out child processes are terminated; the UI reports failure rather than hanging indefinitely.</p></article>
          <article><header><ShieldCheck size={18} /><strong>Data boundary</strong></header><ul><li>Source bytes are locally inspected first.</li><li>Codex receives creator chat and bounded candidate context.</li><li>Provider upload, spend, rendering, durable memory, and publishing are separate governed effects.</li><li>ReelBrain never reads <code>~/.codex/auth.json</code>.</li></ul></article>
        </div>
        <div className="workspace-path"><strong>Workspace</strong><code>{health?.workspace}</code></div>
      </section>
    );
  }

  const view = activeView === "Home" ? HomeView() : activeView === "Projects" ? ProjectsView() : activeView === "Memory" ? MemoryEvidenceView() : activeView === "Review" ? ReviewView() : SettingsView();

  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand-lockup" onClick={() => setActiveView("Home")}><BrainMark /><span>Reel<span>Brain</span></span></button>
        <div className="topbar-divider" />
        <button className="project-switcher" onClick={() => setActiveView("Projects")}><span>{run.projectTitle}</span><ChevronDown size={17} /></button>
        <button className={`connection-pill ${connection.connected ? "is-connected" : ""}`} onClick={connection.connected ? refreshConnection : handleConnect} title={connection.detail}>
          {connecting ? <LoaderCircle size={14} className="spin" /> : <span className="connection-dot" />}{runtimeLabel}
        </button>
        <div className="privacy-pill"><ShieldCheck size={15} /> Source video stays local until approved</div>
        <div className="topbar-spacer" />
        <button className="review-button" disabled={!pendingReviewOutputs.length} onClick={() => setActiveView("Review")}>{pendingReviewOutputs.length ? `Review ${pendingReviewOutputs.length} drafts` : "Review caught up"}<ArrowRight size={18} /></button>
      </header>

      <aside className="sidebar">
        <nav>
          {([
            ["Home", Home],
            ["Projects", FolderKanban],
            ["Memory", Heart],
            ["Review", Eye],
            ["Settings", Settings],
          ] as const).map(([label, Icon]) => (
            <button key={label} className={`nav-item ${activeView === label ? "is-active" : ""}`} onClick={() => setActiveView(label)}><Icon size={19} /><span>{label}</span></button>
          ))}
        </nav>
        <button className="sidebar-profile" onClick={() => setActiveView("Settings")}><CircleUserRound size={34} /><span className="online-dot" /><span>Founder</span><ChevronDown size={16} className="profile-chevron" /></button>
      </aside>

      <div className="view-container">{view}</div>

      {agentEditorDraft && (
        <div className="agent-editor-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget && !agentSaving) setAgentEditorDraft(null); }}>
          <section className="agent-editor-sheet" role="dialog" aria-modal="true" aria-labelledby="agent-editor-title">
            <header>
              <div><span>Editing persona · {agentEditorDraft.id}</span><h2 id="agent-editor-title">Customize this agent</h2></div>
              <button className="small-icon" disabled={agentSaving} onClick={() => setAgentEditorDraft(null)} aria-label="Close agent editor"><X size={17} /></button>
            </header>
            <label>
              <span>Agent name <em>{agentEditorDraft.name.length}/48</em></span>
              <input autoFocus value={agentEditorDraft.name} maxLength={48} onChange={(event) => setAgentEditorDraft((current) => current ? { ...current, name: event.target.value } : current)} />
            </label>
            <label>
              <span>Persona <em>{agentEditorDraft.role.length}/280</em></span>
              <textarea value={agentEditorDraft.role} maxLength={280} onChange={(event) => setAgentEditorDraft((current) => current ? { ...current, role: event.target.value } : current)} />
            </label>
            <label className="system-prompt-field">
              <span>System prompt <em>{agentEditorDraft.systemPrompt.length}/6000</em></span>
              <textarea value={agentEditorDraft.systemPrompt} maxLength={6000} onChange={(event) => setAgentEditorDraft((current) => current ? { ...current, systemPrompt: event.target.value } : current)} />
              <small>Applied to direct mentions, @team consultation, and governed editorial fan-out. The governed agent ID remains stable.</small>
            </label>
            <div className="agent-tool-access">
              <span><Wrench size={14} /> Assigned semantic tools</span>
              <div>{AGENTS.find((agent) => agent.id === agentEditorDraft.id)?.tools.map((tool) => <code key={tool}>{tool}</code>)}</div>
              <small>Tool implementations remain governed by ACP. New or changed tools require the creator approval and test lifecycle.</small>
            </div>
            <footer>
              <button disabled={agentSaving} onClick={() => setAgentEditorDraft(null)}>Cancel</button>
              <button className="save-agent" disabled={agentSaving || !agentEditorDraft.name.trim() || !agentEditorDraft.role.trim() || !agentEditorDraft.systemPrompt.trim()} onClick={() => void saveAgentProfile()}>{agentSaving ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Save persona</button>
            </footer>
          </section>
        </div>
      )}

      {tasteEditorDraft && (
        <div className="agent-editor-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget && !memoryBusy) setTasteEditorDraft(null); }}>
          <section className="agent-editor-sheet taste-editor-sheet" role="dialog" aria-modal="true" aria-labelledby="taste-editor-title">
            <header>
              <div><span>Creator-approved taste · {tasteEditorDraft.category}</span><h2 id="taste-editor-title">Edit remembered taste</h2></div>
              <button className="small-icon" disabled={memoryBusy} onClick={() => setTasteEditorDraft(null)} aria-label="Close taste editor"><X size={17} /></button>
            </header>
            <label>
              <span>Preference <em>{tasteEditorDraft.value.length}/500</em></span>
              <textarea autoFocus value={tasteEditorDraft.value} maxLength={500} onChange={(event) => setTasteEditorDraft((current) => current ? { ...current, value: event.target.value } : current)} />
            </label>
            <div className="taste-scope-heading"><span>Applies when</span><small>Leave a field empty to apply across that dimension.</small></div>
            <div className="taste-scope-grid">
              <label><span>Output mode</span><input placeholder="short, long-form…" value={tasteEditorDraft.scope.outputMode ?? ""} onChange={(event) => setTasteEditorDraft((current) => current ? { ...current, scope: { ...current.scope, outputMode: event.target.value || null } } : current)} /></label>
              <label><span>Content kind</span><input placeholder="technical, tutorial…" value={tasteEditorDraft.scope.contentKind ?? ""} onChange={(event) => setTasteEditorDraft((current) => current ? { ...current, scope: { ...current.scope, contentKind: event.target.value || null } } : current)} /></label>
              <label><span>Language</span><input placeholder="bilingual, Korean…" value={tasteEditorDraft.scope.language ?? ""} onChange={(event) => setTasteEditorDraft((current) => current ? { ...current, scope: { ...current.scope, language: event.target.value || null } } : current)} /></label>
            </div>
            <div className="taste-principle-note"><ShieldCheck size={16} /><p><strong>Behavioral prior, not evidence.</strong> Agents may use this to choose style and behavior, but it cannot establish what the source video says.</p></div>
            <footer>
              <button disabled={memoryBusy} onClick={() => setTasteEditorDraft(null)}>Cancel</button>
              <button className="save-agent" disabled={memoryBusy || !tasteEditorDraft.value.trim()} onClick={() => void saveTastePreference()}>{memoryBusy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Save taste</button>
            </footer>
          </section>
        </div>
      )}

      {tasteForgetTarget && (
        <div className="agent-editor-scrim confirmation-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget && !memoryBusy) setTasteForgetTarget(null); }}>
          <section className="taste-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="taste-delete-title" aria-describedby="taste-delete-description">
            <span className="delete-symbol"><Trash2 size={19} /></span>
            <h2 id="taste-delete-title">Forget this preference?</h2>
            <p id="taste-delete-description">“{tasteForgetTarget.value}” will be removed permanently. ReelBrain keeps only a content-free tombstone so Sleep cannot resurrect it.</p>
            <div>
              <button disabled={memoryBusy} onClick={() => setTasteForgetTarget(null)}>Cancel</button>
              <button className="confirm-delete" disabled={memoryBusy} onClick={() => void confirmForgetPreference()}>{memoryBusy ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />} Forget permanently</button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

export default App;
