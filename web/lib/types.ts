/** Public API contracts consumed by the browser (mirrors backend schemas). */

export type UserStatus = "pending_approval" | "approved" | "rejected";
export type UserRole = "admin" | "member";

export type AuthUser = {
  id: string;
  full_name: string;
  email: string;
  company_id: string | null;
  status: UserStatus;
  role: UserRole;
  approved_at: string | null;
  created_at: string;
};

export type AuthSession = {
  user: AuthUser;
  token: string;
  token_expires_at: string;
};

export type AccountList = {
  items: AuthUser[];
  total: number;
  pending: number;
};

export type Brand = {
  id: string;
  company_id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  cta_config: Record<string, unknown>;
  domain: string | null;
  industry: string | null;
  language: string;
  video_width: number;
  video_height: number;
  tone: string | null;
  visual_config: Record<string, unknown>;
  status: string;
};

export type MemorySnapshot = {
  id: string;
  extracted_at: string;
  source_document_count: number;
  model: string | null;
};

export type BrandMemoryPlatform = {
  id: string;
  name: string;
  base_url: string | null;
  platform_type: string | null;
  status: string | null;
  snapshot: MemorySnapshot | null;
  knowledge: Record<string, unknown> | null;
  history: MemorySnapshot[];
};

export type BrandMemoryAnalysis = {
  selected_topic?: string | null;
  selection_reason?: string | null;
  brand_fit?: number | null;
  candidate_count?: number | null;
  duplicate_count?: number | null;
  source_count?: number | null;
  platform_count?: number | null;
  sources?: Array<{
    platform_id?: string;
    source_url?: string;
    evidence?: string;
  }>;
  supporting_points?: Array<{
    topic?: string;
    description?: string;
    source_url?: string;
    brand_fit?: number;
  }>;
};

export type BrandMemory = {
  brand_id: string;
  brand_name: string;
  language: string;
  has_knowledge: boolean;
  platform_count: number;
  source_count: number;
  snapshot_count: number;
  analysis: BrandMemoryAnalysis | null;
  platforms: BrandMemoryPlatform[];
  updated_at: string | null;
};

export type BrandMemoryRefresh = {
  status: string;
  monitor: Record<string, unknown>;
  memory: BrandMemory;
};

export type CharacterImage = {
  key: string;
  platform: string;
  name: string;
  size_bytes: number;
  content_type: string;
  valid: boolean;
  error?: string;
};

export type SceneProvider = {
  provider_id: string;
  display_name: string;
  supports_cancellation: boolean;
  requires_network: boolean;
  daily_quota: number | null;
};

export type ProjectStatus =
  | "draft"
  | "script_generating"
  | "script_ready"
  | "script_failed"
  | "approved"
  | "scenes_prepared"
  | "video_generation_pending"
  | "video_generating"
  | "video_generated"
  | "assembling"
  | "completed"
  | "failed"
  | "cancelled";

export type ContentProject = {
  id: string;
  company_id: string;
  brand_id: string;
  topic: string;
  status: ProjectStatus;
  video_mode: string;
  script_text: string | null;
  script_segments: Record<string, unknown>[] | null;
  scenario: Record<string, unknown> | null;
  scene_options: Record<string, unknown> | null;
  scene_video_provider: string | null;
  assembly_metadata: Record<string, unknown> | null;
  video_title: string | null;
  call_to_action: string | null;
  llm_usage: Record<string, unknown> | null;
  script_workflow_execution_id: string | null;
  video_workflow_execution_id: string | null;
  provider_job_id: string | null;
  provider_status: string | null;
  raw_artifact_id: string | null;
  final_artifact_id: string | null;
  cost_metadata: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkflowExecution = {
  id: string;
  workflow_id: string | null;
  workflow_name: string;
  status: string;
  trigger_type: string | null;
  trigger_reference: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
};

export type WorkflowStep = {
  id: string;
  node_key: string;
  node_type: string;
  status: string;
  attempt: number;
  output: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
};

export type WorkflowStepList = {
  items: WorkflowStep[];
  total: number;
};

export type AgentTaskStatus =
  | "pending"
  | "queued"
  | "awaiting_approval"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type AgentTask = {
  id: string;
  company_id: string;
  agent_key: string;
  capability: string;
  title: string;
  status: AgentTaskStatus;
  priority: number;
  input_payload: Record<string, unknown>;
  result_payload: Record<string, unknown> | null;
  idempotency_key: string | null;
  requested_by: string | null;
  cancel_requested: boolean;
  attempt_count: number;
  max_attempts: number;
  spend_ceiling_usd: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  agent_dispatch_ready: boolean;
};

export type AgentTaskEvent = {
  id: string;
  task_id: string;
  company_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type AgentTaskList = {
  items: AgentTask[];
  total: number;
  limit: number;
  offset: number;
};

export type ScenarioScene = {
  index?: number;
  key?: string;
  narration?: string;
  prompt?: string;
  duration_seconds?: number;
  [key: string]: unknown;
};
