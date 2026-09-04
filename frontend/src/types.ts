export type StatusTone = "ready" | "running" | "review" | "blocked" | "pending";

export type AnalysisRunStage = {
  id: "ingest" | "visual" | "audio" | "report" | "finalize" | string;
  state: "running" | "completed" | "skipped" | "failed" | "interrupted" | string;
  attempt?: number;
  started_at?: string | null;
  finished_at?: string | null;
  elapsed_seconds?: number | null;
  detail?: string | null;
};

export type AnalysisRun = {
  schema_version: number;
  run_id: string;
  project_id: string;
  kind: string;
  state: "queued" | "running" | "cancelling" | "cancelled" | "completed" | "failed" | "interrupted";
  stage: string;
  progress: number;
  attempt: number;
  created_at: string;
  started_at?: string | null;
  updated_at: string;
  finished_at?: string | null;
  request?: { source?: string; profile?: string; [key: string]: unknown };
  stages: AnalysisRunStage[];
  error?: { type?: string; message?: string; retriable?: boolean } | null;
  result?: { status?: string; summary?: string; elapsed_seconds?: number; artifacts?: Record<string, string> } | null;
  links?: { self?: string; project?: string; workspace?: string };
};

export type ProjectSummary = {
  project_id: string;
  source?: string;
  status?: string;
  media?: { review_video?: MediaAsset; shot_count?: number; keyframe_count?: number };
  readiness?: Readiness;
};

export type ProjectDetail = {
  project_id: string;
  manifest?: {
    project_id?: string;
    profile?: string;
    source?: string;
    status?: string;
    artifacts?: Record<string, string>;
    [key: string]: unknown;
  };
  readiness?: Readiness;
  canvas?: { node_count?: number; edge_count?: number; href?: string };
  media?: { shot_count?: number; keyframe_count?: number; href?: string; review_video?: MediaAsset };
  deliverables?: { artifact_count?: number; href?: string };
};

export type MediaAsset = {
  path?: string | null;
  relative_path?: string;
  url?: string | null;
  present?: boolean;
  duration_seconds?: number;
  frame_rate?: number;
  resolution?: string;
  width?: number;
  height?: number;
  aspect_ratio?: number;
  status?: string;
};

export type KeyframeRef = {
  url?: string | null;
  relative_path?: string;
  present?: boolean;
  time?: number;
};

export type AnnotationVerification = "provider_receipt_verified" | "agent_submission_bound" | "human_reviewed" | "unverified";

export type ShotReviewFields = {
  story_beat?: string;
  content_summary?: string;
  content_summary_zh?: string;
  subject?: string;
  subject_zh?: string;
  action?: string;
  action_zh?: string;
  shot_scale?: string;
  camera_angle?: string;
  camera_motion?: string;
  composition?: string;
  onscreen_text?: string;
  dialogue?: string;
  review_notes?: string;
  visual_confidence?: number;
  readiness_status?: "blocked" | "ready" | "rejected";
  boundary_reviewed?: boolean;
};

export type ShotBoundary = {
  id: string;
  edit_version: string;
  shot_id?: string;
  shot_no?: number;
  canvas_node_id?: string;
  start_time?: number;
  end_time?: number;
  duration?: number;
  timecode?: string;
  story_beat?: string;
  story_beat_raw?: string;
  annotation_source?: string;
  annotation_verification?: AnnotationVerification;
  primary_frame_ref?: string;
  keyframes?: KeyframeRef[];
  shot_size?: string;
  angle?: string;
  sound?: string;
  visual_content?: string;
  meaning?: string;
  rhythm?: string;
  readiness_status?: string;
  readiness_reasons?: string[];
  visual_confidence?: number;
  boundary_confidence?: string;
  review_fields?: ShotReviewFields;
};

export type MediaTimeline = {
  project_id: string;
  review_video?: MediaAsset;
  shot_boundaries?: ShotBoundary[];
  keyframes?: KeyframeRef[];
  markers?: Array<Record<string, unknown>>;
  segments?: Array<Record<string, unknown>>;
};

export type CanvasGraph = {
  project_id: string;
  viewport?: { x?: number; y?: number; zoom?: number };
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  version?: string;
};

export type CanvasNode = {
  id: string;
  type: string;
  position?: { x: number; y: number };
  width?: number;
  height?: number;
  data?: Record<string, unknown>;
};

export type CanvasEdge = {
  id: string;
  source: string;
  target: string;
  type?: string;
};

export type Readiness = {
  status?: string;
  score?: number;
  professional_export_allowed?: boolean;
  reasons?: string[];
  summary?: string;
  checks?: Array<{ id?: string; label?: string; status?: string; message?: string }>;
  audio_timeline_available?: boolean | null;
  audio_review_complete?: boolean | null;
  audio_event_count?: number;
  audio_requires_review_count?: number;
  audio_intelligence_binding?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type DeliverableArtifact = {
  id: string;
  group?: string;
  label: string;
  url?: string | null;
  content_type?: string;
  present?: boolean;
  readiness_status?: string;
  size_bytes?: number;
  preview_url?: string;
};

export type DeliverablesPayload = {
  project_id: string;
  readiness?: Readiness;
  artifacts: DeliverableArtifact[];
  export?: { allowed?: boolean; blocked_reasons?: string[] };
};

export type ExportFormat = "xlsx" | "pdf";

export type ClientExportReceipt = {
  schema_id: "client-export-package/v1";
  state: "current";
  export_id: string;
  receipt_digest: string;
  idempotency_key: string;
  request_digest: string;
  dataset_digest: string;
  source_generation_id: string;
  formats: ExportFormat[];
  settings: {
    language?: string;
    density?: string;
    project_subtitle?: { text?: string };
    accent_color?: string;
    logo?: { path?: string; sha256?: string; media_type?: string } | null;
  };
  outputs: Partial<Record<ExportFormat, { filename: string; sha256: string; size_bytes: number }>>;
  created_at_utc: string;
};

export type ClientExportState = {
  schema_id: "client-export-state/v1";
  status: "absent" | "rendering" | "publishing" | "current" | "failed" | "cancelled";
  request_digest?: string;
  export_id?: string | null;
  reason?: string | null;
  updated_at_utc?: string;
};

export type ExportCenterPayload = {
  schema_id: "client-export-center/v1";
  state: ClientExportState;
  current: {
    lifecycle_state: "current" | "stale";
    receipt: ClientExportReceipt;
    downloads: Partial<Record<ExportFormat, string>>;
  } | null;
  saved: Array<{
    version_id: string;
    export_id: string;
    formats: ExportFormat[];
    created_at_utc: string;
    size_bytes: number;
    downloads: Partial<Record<ExportFormat, string>>;
  }>;
};

export type RuntimeSettings = {
  workspace_path?: string;
  vision_provider?: string;
  openai?: { api_key_configured?: boolean; api_key_masked?: string; base_url?: string; model?: string };
  minimax?: { api_key_configured?: boolean; api_key_masked?: string; api_host?: string };
  readiness_rules?: Record<string, number>;
};

export type DoctorPayload = {
  doctor?: {
    status?: string;
    summary?: string;
    next_actions?: string[];
    checks?: Array<{ name?: string; status?: string; detail?: string }>;
    error?: string | null;
  };
};

export type WorkspaceSnapshot = {
  snapshot_id: string;
  generation_id: string | null;
  project: ProjectDetail;
  canvas: CanvasGraph;
  media: MediaTimeline;
  deliverables: DeliverablesPayload;
};

export type WorkspaceBundle = WorkspaceSnapshot & {
  source: "backend" | "demo";
  gaps: string[];
};

export type AudioEventKind = "voice" | "music" | "sfx" | "silence" | "mixed";
export type AudioReviewStatus = "reviewed" | "rejected" | "needs_work";

export type AudioProposal = {
  label: string;
  text: string;
  language: string;
  speaker_id: string | null;
  voice_role: "voice_over" | "dialogue" | "singing" | "unknown";
  energy: number | null;
  onset_density: number | null;
  estimated_bpm: number | null;
  confidence: number;
  verification: "measured" | "machine_estimated" | "model_interpreted" | "human_draft" | "human_reviewed";
};

export type AudioEventReview = {
  status: AudioReviewStatus;
  expected_proposal_sha256: string;
  overrides: Partial<Omit<AudioProposal, "verification">>;
  review_notes: string;
  verification: "human_draft" | "human_reviewed";
};

export type AudioEvent = {
  event_id: string;
  start_time: number;
  end_time: number;
  kind: AudioEventKind;
  source_id: string;
  proposal: AudioProposal;
  proposal_sha256: string;
  review: AudioEventReview | null;
  effective_proposal: AudioProposal | null;
  identity_status: "unknown" | "machine_estimated" | "human_reviewed";
  evidence_ref: string;
  requires_review: boolean;
  shot_link?: {
    shot_id: string;
    overlap_start: number;
    overlap_end: number;
    overlap_seconds: number;
    [key: string]: unknown;
  };
};

export type AudioCapability = {
  status: "produced" | "unknown" | "failed" | "skipped";
  source_id: string | null;
  reason: string | null;
};

export type AudioReviewPage = {
  schema_id: "audio-review/v1";
  project_id: string;
  available: boolean;
  generation_id: string | null;
  source_binding: Record<string, unknown> | null;
  capabilities: Record<string, AudioCapability>;
  sources: Array<Record<string, unknown>>;
  events: AudioEvent[];
  page: { offset: number; limit: number; total: number; next_offset: number | null };
  review_counts: Partial<Record<"unreviewed" | AudioReviewStatus, number>>;
  requires_review_count: number | null;
  counts_scope: "all audio events";
  shot_context: Record<string, unknown> | null;
  data_trust: string;
  reason?: string;
};

export type AudioReviewRequest = {
  expected_generation_id: string;
  expected_proposal_sha256: string;
  status: AudioReviewStatus;
  overrides?: Partial<Omit<AudioProposal, "verification">>;
  review_notes?: string;
  confirm_operator_review: true;
};

export type AudioReviewResult = {
  schema_id: "audio-review/v1";
  project_id: string;
  review_saved: true;
  changed: boolean;
  generation_id: string;
  previous_generation_id?: string;
  event: AudioEvent;
  report_regeneration_required: boolean;
  exports_generated: false;
  cleanup_required?: boolean;
};
