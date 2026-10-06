// API types mirroring backend schemas.

export type Persona = "business" | "power";
export type Role = "business" | "power" | "admin";
export type AccountClass = "standard" | "demo";
export type DemoExperience = "business" | "power";

export interface User {
  id: string;
  email: string;
  name: string | null;
  persona: Persona | null;
  role: Role;
  account_class: AccountClass;
  experience_view: DemoExperience | null;
  can_demo_switch: boolean;
  allowed_demo_experiences: DemoExperience[];
  /** View-only admin visibility (product decision, 4 Sep 2026). */
  admin_readonly: boolean;
  use_case: string | null;
  onboarding_completed: boolean;
  tour_completed: boolean;
  persona_upgrade_requested: boolean;
  created_at: string;
}

// Home workbench aggregate (beta-usability R1 / FSD B14)
export interface Workbench {
  reviews: { is_reviewer: boolean; pending: number };
  expiring_deployments: {
    project_id: string;
    project_name: string | null;
    app_url: string | null;
    expires_at: string;
    hours_left: number;
  }[];
  failed_builds: {
    project_id: string;
    project_name: string | null;
    build_id: string;
    error_code: string | null;
    created_at: string;
  }[];
  awaiting_resubmission: {
    project_id: string;
    project_name: string | null;
    level: string | null;
    notes: string | null;
    decided_at: string | null;
  }[];
  ops?: { active_alerts: number; max_severity: "info" | "warning" | "critical" | null };
}

// Global search (beta-usability R2 / FSD B12)
export interface SearchResults {
  query: string;
  groups: {
    projects: { id: string; name: string; status: string }[];
    sessions: { id: string; title: string; updated_at: string }[];
    samples: { id: string; title: string; category: string; complexity: string }[];
    specs?: { project_id: string; project_name: string | null; type: string; version: number }[];
  };
}

export interface UserStats {
  projects: number;
  specs: number;
  sessions: number;
  deployments: number;
}

export interface ChatSession {
  id: string;
  title: string;
  status: string;
  model_id: string | null;
  project_id: string | null;
  template_id: string | null;
  mode?: "freeform" | "guided";
  guided_state?: GuidedState | null;
  // Collaboration (S7): creator attribution for shared-project sessions
  creator_name?: string | null;
  is_mine?: boolean | null;
  project_name?: string | null;
  /** Requested studio-rail overrides (S18) — clamps applied server-side. */
  params?: { model_id?: string; temperature?: number; max_tokens?: number } | null;
  /** Brownfield substrate presence — never the content (brownfield-substrate spec). */
  has_substrate?: boolean;
  substrate_source?: string | null;
  substrate_size?: number;
  created_at: string;
  updated_at: string;
}

// ------------------------------------------------------------------ studio (S18)

export interface MetaFeatures {
  studio_enabled: boolean;
  studio: boolean;
  studio_dark: boolean;
  /** C1 connector consumption — ships dark; activates the spec signal. */
  connectors_enabled?: boolean;
}

export interface PromptContextResp {
  segments: { label: string; content: string }[];
  model_id: string;
  params: Record<string, unknown>;
}

export interface EffectiveParamsResp {
  requested: { model_id: string | null; temperature: number | null; max_tokens: number | null };
  effective: { model_id: string; temperature: number | null; max_tokens: number };
  clamped_by: { model_id: string | null; temperature: string | null; max_tokens: string | null };
  allowed_models: string[];
  template_name: string | null;
}

export type DocType = "requirements" | "design" | "tasks";
export const DOC_TYPES: DocType[] = ["requirements", "design", "tasks"];

export interface GenerationDoc {
  type: DocType;
  status: "pending" | "generating" | "done" | "failed";
  spec_id?: string;
  version?: number;
  error?: string;
}

export interface Generation {
  id: string;
  session_id: string;
  project_id: string | null;
  status: "running" | "done" | "failed";
  docs: GenerationDoc[];
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface TemplateUser {
  id: string;
  name: string;
  version: number;
  description: string | null;
  category: string;
  starter_prompts: string[];
  allowed_model_labels: string[];
}

export interface TemplateAdmin {
  id: string;
  name: string;
  version: number;
  description: string | null;
  category: string;
  status: "draft" | "active" | "deprecated";
  guardrails: Record<string, unknown> & {
    model?: {
      allowed_models?: string[];
      max_tokens?: number;
      temperature?: { min: number; max: number };
      top_p?: { min: number; max: number };
    };
  };
  scaffolding: { starter_prompts?: string[]; required_spec_sections?: string[] };
  usage_count: number;
  active_project_count?: number;
  created_at: string;
  updated_at: string;
}

export interface ModelRegistryEntry {
  id: string;
  label: string;
  tier: string;
  /** "aws" | "external" — external entries are custom endpoints (S17). */
  source?: string;
  provider?: string;
  provider_name?: string | null;
  base_model_id?: string | null;
  status?: string;
  availability?: string;
  selectable?: boolean;
  disabled_reason?: string | null;
  converse_supported?: boolean;
  supports_streaming?: boolean;
  input_modalities?: string[];
  output_modalities?: string[];
  inference_type?: string | null;
  inference_profiles?: string[];
  routing_scope?: string | null;
  access_status?: string | null;
  pricing?: {
    version?: number;
    status?: string;
    input_usd_per_1k?: number | null;
    output_usd_per_1k?: number | null;
  } | null;
  catalog_source?: string | null;
  catalog_stale?: boolean;
  catalog_error?: string | null;
  catalog_updated_at?: string | null;
}

export interface SpecDraftOut {
  content: string;
  updated_at: string;
}

export interface SpecSaveResult {
  spec: Spec;
  warnings: string[];
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  model_id?: string | null;
  sk: string;
  streaming?: boolean;
  error?: string;
}

export interface Spec {
  id: string;
  project_id: string;
  version: number;
  type: string;
  content: string;
  model_id: string | null;
  origin: "generated" | "edited" | "rollback";
  created_at: string;
}

export interface SpecVersionInfo {
  id: string;
  version: number;
  origin: "generated" | "edited" | "rollback";
  model_id: string | null;
  created_by_name?: string | null;
  created_at: string;
}

export interface DocSpecState {
  latest: Spec | null;
  versions: SpecVersionInfo[];
}

export interface FullSpecResponse {
  requirements: DocSpecState;
  design: DocSpecState;
  tasks: DocSpecState;
}

export interface Deployment {
  id: string;
  project_id: string;
  status: string;
  build_id?: string | null; // codegen provenance (S8)
  // ---- S11 deployment maturity ----
  health?: "unknown" | "healthy" | "degraded";
  last_health_at?: string | null;
  expires_at?: string | null;
  superseded_at?: string | null;
  lease_state?: {
    budget_used_usd?: number | null;
    budget_cap_usd?: number | null;
    expires_at?: string | null;
    status?: string | null;
  } | null;
  created_by_name?: string | null;
  stack_name: string | null;
  app_url: string | null;
  error: string | null;
  timeline: { phase: string; at: string; detail?: string | null }[];
  resources: { logical_id: string; type: string; status: string }[];
  lease_account_id: string | null;
  lease_external_id: string | null;
  // B20 R1.4: custody posture — drives Enclave/Testbed naming
  mode?: "full_governance" | "testbed";
  deployed_at: string | null;
  torn_down_at: string | null;
  created_at: string;
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  status: string;
  origin: string;
  template_id: string | null;
  template_name: string | null;
  template_deprecated: boolean;
  forked_from_sample_id: string | null;
  forked_from_title: string | null;
  spec_version_count: number;
  last_activity_at: string | null;
  risk_level: string | null;
  cost_mtd_usd: number | null;
  budget_override_usd?: number | null;
  my_role?: "owner" | "editor" | "viewer" | null;
  team_id?: string | null; // S15-02: null = personal project
  team_name?: string | null;
  /** Composable agents: declared dependency graph (server-enriched status). */
  composition?: {
    dependencies?: {
      slug: string;
      project_id: string;
      name?: string;
      deployment_status?: string;
    }[];
    orchestration?: string | null;
    depth?: number;
  } | null;
  created_at: string;
  updated_at: string;
  deployment: Deployment | null;
}

// ------------------------------------------------------- collaboration (S7)

export type ProjectRole = "owner" | "editor" | "viewer";

export interface MemberInfo {
  user_id: string;
  email: string;
  name: string | null;
  role: "viewer" | "editor";
  added_at: string;
}

export interface MembersResponse {
  owner: { user_id: string; email: string | null; name: string | null };
  members: MemberInfo[];
  my_role: ProjectRole | null;
}

export interface CommentThread {
  id: string;
  doc_type: DocType;
  anchor: string;
  anchor_text: string;
  parent_id: string | null;
  author_id: string;
  author_name: string;
  body: string;
  resolved: boolean;
  created_at: string;
  replies: CommentThread[];
}

export interface PresenceUser {
  user_id: string;
  name: string;
  email: string;
  surface: string;
  last_seen_at: string;
}

// -------------------------------------------------- marketplace submissions

export interface Submission {
  id: string;
  title: string;
  description: string;
  category: string;
  status: "submitted" | "rejected" | "withdrawn" | "draft" | "published" | "archived";
  source_project_id: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_feedback: string | null;
  resubmission_of: string | null;
  author_name: string | null;
  author_email: string | null;
  project_name: string | null;
}

export interface ProjectList {
  items: Project[];
  total: number;
  archived_count: number;
  page: number;
  page_size: number;
}

export interface ActivityItem {
  id: string;
  created_at: string;
  category: string;
  action: string;
  actor_email: string | null;
  detail: Record<string, unknown>;
}

export interface ActivityList {
  items: ActivityItem[];
  total: number;
  page: number;
  page_size: number;
}

// ------------------------------------------------------------- marketplace

export interface SampleCard {
  id: string;
  title: string;
  description: string;
  category: string;
  complexity: "beginner" | "intermediate" | "advanced";
  models_used: string[];
  fork_count: number;
  view_count: number;
  published_at: string | null;
  template_id: string | null;
  status?: string | null; // admin listings only
  contributed_by?: string | null; // submission-originated samples (S7)
}

export interface SampleDetail extends SampleCard {
  long_description: string | null;
  spec_snapshot: Partial<Record<"requirements_md" | "design_md" | "tasks_md", string>>;
  assets: { screenshots?: string[]; demo_url?: string | null };
  keywords: string[];
  metadata_extra: {
    est_build_usd?: number;
    est_run_usd_month?: number;
    tech_stack?: string[];
  };
  author_name: string | null;
  template_name: string | null;
  template_deprecated: boolean;
}

export interface SampleAdmin extends SampleDetail {
  status: "draft" | "submitted" | "approved" | "published" | "archived";
  created_at: string;
  updated_at: string;
}

export interface SampleList {
  items: SampleCard[];
  total: number;
  page: number;
  page_size: number;
}

export interface CategoryCount {
  category: string;
  count: number;
}

export interface ForkResult {
  project_id: string;
  warnings: string[];
}

// ------------------------------------------------------------------- admin

export interface AdminUser {
  id: string;
  email: string;
  name: string | null;
  role: Role;
  role_source: "sso" | "admin";
  persona: Persona | null;
  status: "active" | "suspended";
  account_class: AccountClass;
  experience_view: DemoExperience | null;
  admin_readonly: boolean;
  persona_upgrade_requested: boolean;
  budget_override_usd: number | null;
  onboarding_completed: boolean;
  created_at: string;
  project_count: number;
  last_active_at: string | null;
}

export interface AdminUserList {
  items: AdminUser[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminUserStats {
  total: number;
  active_30d: number;
  business: number;
  power: number;
  admins: number;
}

export interface AuditEntry {
  id: string;
  created_at: string;
  source: "audit" | "model";
  category: string;
  action: string;
  actor_id: string | null;
  actor_email: string | null;
  resource_type: string | null;
  resource_id: string | null;
  project_id: string | null;
  http_status: number | null;
  summary: string | null;
  detail?: Record<string, unknown>;
}

export interface AuditList {
  items: AuditEntry[];
  total: number;
  page: number;
  page_size: number;
}

// ---------------------------------------------------------- governance (S4)

export interface ModelControls {
  model_allowlist: string[];
  // S9 codegen engine; "kiro" is the legacy value older rows may still return
  codegen?: { provider?: "internal" | "runner" | "kiro" };
  param_bounds: {
    temperature?: { min: number; max: number };
    top_p?: { min: number; max: number };
    max_tokens?: number;
    max_context?: number;
  };
  rate_limits: {
    per_user_rpm?: number;
    per_project_rpm?: number;
    platform_rpm?: number;
  };
  cost: {
    platform_budget_usd?: number | null;
    default_user_cap_usd?: number;
    default_project_cap_usd?: number;
    alert_thresholds?: number[];
    at_cap?: "alert" | "block";
  };
  security?: {
    admin_mfa_required?: boolean;
    pii_redaction?: boolean;
    /** Where masking applies when on: chat exempted at "non_interactive". */
    pii_redaction_scope?: "all" | "non_interactive";
  };
  deployment_policies?: {
    admissions_paused?: boolean;
    max_concurrent_per_user?: number;
    max_concurrent_platform?: number;
    provider_capacity_buffer?: number;
    allowed_regions?: string[];
    default_ttl_hours?: number;
    max_ttl_hours?: number;
    per_deployment_budget_usd?: number;
    max_active_deployments_per_user?: number;
    testbed_enabled?: boolean;
    testbed_gate_mode?: "advisory" | "off";
    testbed_session_hours?: number;
    default_mode?: "full_governance" | "testbed";
    require_endpoint_auth?: "default" | "always";
  };
  updated_at?: string | null;
}

export interface RiskFactor {
  score: number;
  rationale: string;
  weight: number;
}

export interface RiskAssessment {
  id: string;
  project_id: string;
  project_name: string | null;
  owner_email: string | null;
  score: number | null;
  level: "low" | "medium" | "high" | null;
  factors: Record<string, RiskFactor>;
  status: "scored" | "error";
  decision: "auto_approved" | "pending" | "approved" | "rejected" | null;
  decided_at: string | null;
  notes: string | null;
  error?: string | null;
  created_at: string;
}

export interface RiskList {
  items: RiskAssessment[];
  total: number;
  pending: number;
  approved_30d: number;
  rejected_30d: number;
  page: number;
  page_size: number;
}

export interface ProjectRisk {
  assessed: boolean;
  current?: boolean;
  score?: number | null;
  level?: string | null;
  decision?: string | null;
  status?: string;
  factors?: Record<string, RiskFactor>;
  notes?: string | null;
  created_at?: string;
  endpoint_auth?: "key_required" | "open"; // displayed, never risk-scored
  endpoint_auth_source?: "default" | "explicit_key" | "explicit_public" | "conflict";
  // B20 R1.3: gate stance per deploy intent + form preselect
  gate_modes?: {
    full_governance: "enforce";
    testbed: "advisory" | "off" | "disabled";
  };
  default_mode?: "full_governance" | "testbed";
  // B17: policy version this assessment was scored under vs the current one
  policy_version?: number;
  current_policy_version?: number;
}

// B17: admin-tuned risk policy (versioned; v1 = platform defaults)
export interface RiskPolicy {
  policy_version: number;
  auto_approve_low: boolean;
  band_low_max: number;
  band_medium_max: number;
  weights: Record<string, number>;
  weights_effective: Record<string, number>;
  anchors: Record<string, string>;
  default_anchors: Record<string, string>;
  is_default: boolean;
}

// B20 R2: vended Testbed session — shown once, never persisted
export interface TestbedCredentials {
  deployment_id: string;
  account_id: string;
  session_name: string;
  access_key_id: string;
  secret_access_key: string;
  session_token: string;
  expires_at: string;
  region: string;
  console_url: string | null;
}

export interface CostDashboard {
  period: string;
  total_usd: number;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  unpriced_calls: number;
  budget_usd: number | null;
  projected_usd: number;
  daily: { date: string; usd: number }[];
  by_model: { model_id: string; usd: number }[];
  top_spenders: { user_id: string | null; email: string; projects: number; calls: number; tokens: number; usd: number }[];
}

export interface CostBreakdown {
  period: string;
  group_by: string;
  items: Record<string, string | number | null>[];
}

// -------------------------------------------------------------- guided (S4)

export interface GuidedClarifyItem {
  id: string;
  round: number;
  question: string;
  answer: string | null;
  skipped: boolean;
}

export interface GuidedState {
  step: "use_case" | "context" | "behavior" | "clarify" | "generate" | "done";
  answers: {
    use_case?: { problem: string; category: string; category_other: string | null };
    context?: { audience: string; data_sources: string[]; sensitive_data: string };
    behavior?: { key_actions: string; constraints: string | null };
  };
  clarification: { rounds: number; questions_total: number; items: GuidedClarifyItem[] };
  assumptions: string[];
  outcome: string | null;
}

// ------------------------------------------------------------ codegen (S8)

export interface BuildValidationFinding {
  check: string;
  path: string;
  message: string;
}

// Spec-conformance report (B7/B23): deterministic field/literal violations
// gate; model-review verdicts remain advisory.
export interface ConformanceVerdict {
  source: "deterministic" | "review";
  check?: string;
  verdict: "met" | "violated" | "unverifiable";
  criterion: string;
  evidence: string;
}

export interface ConformanceReport {
  status: "ok" | "unavailable";
  model_id?: string | null;
  summary?: { met: number; violated: number; unverifiable: number };
  verdicts?: ConformanceVerdict[];
  review_error?: string;
  error?: string;
}

export interface CodegenBuild {
  id: string;
  project_id: string;
  status:
    | "queued"
    | "dispatched"
    | "generating"
    | "validating"
    | "ready"
    | "failed"
    | "cancelled";
  provider: string;
  external_job_id?: string | null; // S9: CodeBuild job id when provider=runner
  retried?: boolean;
  artifact_profile?: "inline-cfn" | "cdk-app"; // S10
  phase_detail: string | null;
  spec_hash: string | null;
  content_hash: string | null;
  manifest: {
    app_name?: string;
    architecture_notes?: string;
    files?: { path: string; sha256: string; bytes: number }[];
    validation?: { findings: BuildValidationFinding[]; unenforceable_patterns: string[] };
    conformance?: ConformanceReport;
    endpoint_auth?: {
      mode: "key_required" | "open";
      source: "default" | "explicit_key" | "explicit_public" | "conflict";
    };
    literal_contract?: { version: number; entries: unknown[] };
    inline_retry?: { attempted: boolean; files: string[]; resolved: boolean }; // R2 (B8)
  };
  error: { code: string; message: string; findings?: BuildValidationFinding[] } | null;
  created_by_name: string | null;
  spec_versions: Record<string, number>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BuildArtifactInfo {
  path: string;
  content_hash: string;
  size_bytes: number;
  language: string | null;
}

// ------------------------------------------------------- notifications (S5)

export interface AppNotification {
  id: string;
  type: string;
  title: string;
  body: string;
  link: string | null;
  read_at: string | null;
  created_at: string;
}

export interface NotificationList {
  items: AppNotification[];
  total: number;
  page: number;
  page_size: number;
}

export interface NotificationPrefs {
  prefs: Record<string, { in_app: boolean; email: boolean }>;
  email_enabled: boolean;
}

// ------------------------------------------------------------- alerts (S5)

export interface GovernanceAlert {
  id: string;
  kind: string;
  severity: "info" | "warning" | "critical";
  threshold_pct: number | null;
  message: string;
  status: "active" | "acknowledged";
  created_at: string;
  acknowledged_at: string | null;
}

export interface UsageSummary {
  month: string;
  mtd_usd: number;
  enclave_usd?: number | null; // Enclave infra spend — null while Cost Explorer is dark (S12)
  cap_usd: number | null;
  at_cap: "alert" | "block";
  projects: {
    project_id: string;
    name: string;
    usd: number;
    enclave_usd?: number | null;
    budget_usd: number | null;
  }[];
}
