import { invoke } from "@tauri-apps/api/core";
import type { AgentProfileState, ChatResult, ConnectionStatus, TeamChatResult, ToolApprovalRequest } from "../types";

export async function readCodexStatus(): Promise<ConnectionStatus> {
  return invoke<ConnectionStatus>("codex_status");
}

export async function connectCodex(): Promise<void> {
  await invoke("codex_login");
}

export async function decideToolApproval(input: {
  approvalId: string;
  decision: "approve" | "deny";
  creatorStatement: string;
}): Promise<ToolApprovalRequest> {
  return invoke<ToolApprovalRequest>("decide_tool_approval", { request: input });
}

export async function buildAndTestTool(approvalId: string): Promise<ToolApprovalRequest> {
  return invoke<ToolApprovalRequest>("build_and_test_tool", { request: { approvalId } });
}

export async function deployTestedTool(input: {
  approvalId: string;
  decision: "approve" | "deny";
  creatorStatement: string;
}): Promise<ToolApprovalRequest> {
  return invoke<ToolApprovalRequest>("deploy_tested_tool", { request: input });
}

export async function chatWithReelBrain(input: {
  prompt: string;
  requestId: string;
  imagePaths?: string[];
  context?: string | null;
  threadId?: string | null;
  cwd?: string | null;
}): Promise<ChatResult> {
  return invoke<ChatResult>("codex_chat", { request: input });
}

export async function readAgentProfiles(): Promise<AgentProfileState> {
  return invoke<AgentProfileState>("read_agent_profiles");
}

export async function updateAgentProfile(input: {
  expectedRevision: number;
  id: string;
  name: string;
  role: string;
  systemPrompt: string;
}): Promise<AgentProfileState> {
  return invoke<AgentProfileState>("update_agent_profile", { request: input });
}

export async function chatWithAgent(input: {
  personaId: string;
  prompt: string;
  requestId: string;
  imagePaths?: string[];
  context?: string | null;
  threadId?: string | null;
}): Promise<ChatResult> {
  return invoke<ChatResult>("codex_persona_chat", { request: input });
}

export async function chatWithTeam(input: {
  prompt: string;
  requestId: string;
  imagePaths?: string[];
  context?: string | null;
  threadId?: string | null;
}): Promise<TeamChatResult> {
  return invoke<TeamChatResult>("codex_team_chat", { request: input });
}
