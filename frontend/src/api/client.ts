import type {
  AnalysisRun,
  AudioEventKind,
  AudioReviewPage,
  AudioReviewRequest,
  AudioReviewResult,
  AudioReviewStatus,
  CanvasGraph,
  DeliverablesPayload,
  DoctorPayload,
  ExportCenterPayload,
  ExportFormat,
  MediaTimeline,
  ProjectDetail,
  ProjectSummary,
  RuntimeSettings,
  ShotBoundary,
  ShotReviewFields,
  WorkspaceBundle,
  WorkspaceSnapshot
} from "../types";

type FetchOptions = RequestInit & { timeoutMs?: number };

const apiBase = import.meta.env.VITE_API_BASE ?? "";
let csrfTokenValue: string | undefined;

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly details?: unknown
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

async function responseError(response: Response): Promise<ApiRequestError> {
  let details: unknown;
  let message = `${response.status} ${response.statusText}`;
  try {
    details = await response.json();
    if (typeof (details as { error?: { message?: unknown } })?.error?.message === "string") {
      message = (details as { error: { message: string } }).error.message;
    } else if (typeof (details as { message?: unknown })?.message === "string") {
      message = (details as { message: string }).message;
    }
  } catch {
    // The HTTP status is still actionable when the body is not JSON.
  }
  return new ApiRequestError(message, response.status, details);
}

async function csrfToken(signal?: AbortSignal): Promise<string> {
  if (csrfTokenValue) return csrfTokenValue;
  const response = await fetch(`${apiBase}/api/session`, {
    headers: { Accept: "application/json" },
    signal
  });
  if (!response.ok) throw await responseError(response);
  const payload = (await response.json()) as { csrf_token?: unknown };
  if (typeof payload.csrf_token !== "string" || !payload.csrf_token) {
    throw new ApiRequestError("The local service did not return a CSRF token.");
  }
  csrfTokenValue = payload.csrf_token;
  return csrfTokenValue;
}

async function isInvalidCsrfResponse(response: Response): Promise<boolean> {
  if (response.status !== 403) return false;
  try {
    const payload = (await response.clone().json()) as { error?: { message?: unknown } };
    return payload.error?.message === "Invalid CSRF token";
  } catch {
    return false;
  }
}

async function request<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 12_000);
  const headers = new Headers(options.headers);
  const method = (options.method ?? "GET").toUpperCase();

  try {
    const mutation = method !== "GET" && method !== "HEAD";
    if (mutation) {
      if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      headers.set("X-VEW-CSRF", await csrfToken(controller.signal));
    }
    const send = () => fetch(`${apiBase}${path}`, {
      ...options,
      headers,
      signal: controller.signal
    });
    let response = await send();
    if (mutation && await isInvalidCsrfResponse(response)) {
      csrfTokenValue = undefined;
      headers.set("X-VEW-CSRF", await csrfToken(controller.signal));
      response = await send();
    }
    if (!response.ok) throw await responseError(response);
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiRequestError("The local service did not respond in time. Check the terminal and retry.");
    }
    if (error instanceof ApiRequestError) throw error;
    throw new ApiRequestError(
      "The local service is unavailable. Start `analyze-video serve` and retry.",
      undefined,
      error
    );
  } finally {
    window.clearTimeout(timeout);
  }
}

export function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}

export function assetUrl(url?: string | null): string {
  if (!url) return "";
  if (/^(https?:|data:|blob:)/.test(url)) return url;
  if (apiBase && url.startsWith("/")) return `${apiBase}${url}`;
  return url;
}

export function apiErrorCode(error: unknown): string | undefined {
  if (!(error instanceof ApiRequestError) || typeof error.details !== "object" || error.details === null) return undefined;
  const envelope = error.details as { error?: { details?: { code?: unknown } } };
  return typeof envelope.error?.details?.code === "string" ? envelope.error.details.code : undefined;
}

export type AudioReviewQuery = {
  offset?: number;
  limit?: number;
  kind?: AudioEventKind;
  review_status?: "unreviewed" | AudioReviewStatus | "needs_review";
  shot_id?: string;
  expected_generation_id?: string;
};

export function getAudioReview(projectId: string, query: AudioReviewQuery = {}): Promise<AudioReviewPage> {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) parameters.set(key, String(value));
  }
  const suffix = parameters.size ? `?${parameters}` : "";
  return request<AudioReviewPage>(`/api/projects/${encodeURIComponent(projectId)}/audio${suffix}`, {
    timeoutMs: 120_000
  });
}

export function saveAudioReview(
  projectId: string,
  eventId: string,
  review: AudioReviewRequest
): Promise<AudioReviewResult> {
  return request<AudioReviewResult>(
    `/api/projects/${encodeURIComponent(projectId)}/audio/events/${encodeURIComponent(eventId)}/review`,
    { method: "PATCH", timeoutMs: 120_000, body: JSON.stringify(review) }
  );
}

export async function listProjects(): Promise<{ projects: ProjectSummary[]; backend: true }> {
  const data = await request<{ projects: ProjectSummary[] }>("/api/projects");
  return { projects: data.projects ?? [], backend: true };
}

export async function getProject(projectId: string): Promise<{ project: ProjectDetail; backend: true }> {
  const project = await request<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`);
  return { project, backend: true };
}

export async function getCanvas(projectId: string): Promise<{ canvas: CanvasGraph; backend: true }> {
  const canvas = await request<CanvasGraph>(`/api/projects/${encodeURIComponent(projectId)}/canvas`);
  return { canvas, backend: true };
}

export async function getMedia(projectId: string): Promise<{ media: MediaTimeline; backend: true }> {
  const media = await request<MediaTimeline>(`/api/projects/${encodeURIComponent(projectId)}/media`);
  return { media, backend: true };
}

export async function getDeliverables(projectId: string): Promise<{ deliverables: DeliverablesPayload; backend: true }> {
  const deliverables = await request<DeliverablesPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/deliverables`
  );
  return { deliverables, backend: true };
}

export function getExportCenter(projectId: string): Promise<ExportCenterPayload> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports`, {
    timeoutMs: 30_000
  });
}

export function getClientExportState(projectId: string): Promise<import("../types").ClientExportState> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports/state`, {
    timeoutMs: 30_000
  });
}

export function generateClientExport(
  projectId: string,
  body: {
    formats: ExportFormat[];
    settings: Record<string, string>;
    idempotency_key: string;
  }
): Promise<import("../types").ClientExportReceipt> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports`, {
    method: "POST",
    timeoutMs: 5 * 60 * 1000,
    body: JSON.stringify(body)
  });
}

export function cancelClientExport(projectId: string, requestDigest: string): Promise<{ status: string }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports/cancel`, {
    method: "POST",
    body: JSON.stringify({ request_digest: requestDigest })
  });
}

export function saveClientExport(projectId: string, versionId: string): Promise<import("../types").ClientExportReceipt> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports/save`, {
    method: "POST",
    timeoutMs: 5 * 60 * 1000,
    body: JSON.stringify({ version_id: versionId })
  });
}

export function deleteClientExport(projectId: string, versionId: string): Promise<{ status: string; version_id: string }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports/saved/${encodeURIComponent(versionId)}`, {
    method: "DELETE"
  });
}

export function recoverClientExports(projectId: string): Promise<{ status: string }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/exports/recover`, {
    method: "POST",
    body: "{}"
  });
}

export async function loadWorkspace(projectId: string): Promise<WorkspaceBundle> {
  const snapshot = await request<WorkspaceSnapshot>(
    `/api/projects/${encodeURIComponent(projectId)}/workspace`
  );
  return {
    ...snapshot,
    source: "backend",
    gaps: []
  };
}

export async function updateShotReview(
  projectId: string,
  shotId: string,
  expectedShotDigest: string,
  review: ShotReviewFields
): Promise<{
  review_saved: true;
  report_regeneration_required: true;
  shot: ShotBoundary;
  saved_shot_digest: string;
}> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/shots/${encodeURIComponent(shotId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      ...review,
      expected_shot_digest: expectedShotDigest
    })
  });
}

export async function regenerateProjectReport(projectId: string): Promise<WorkspaceBundle> {
  const response = await request<{ workspace: WorkspaceSnapshot }>(
    `/api/projects/${encodeURIComponent(projectId)}/report`,
    {
      method: "POST",
      timeoutMs: 5 * 60 * 1000,
      body: "{}"
    }
  );
  return {
    ...response.workspace,
    source: "backend",
    gaps: []
  };
}

export type CodexAnalysisStatus = {
  status: "absent" | "prepared" | "applied" | "stale";
  request_id?: string;
  selected_shot_count?: number;
  reason?: string;
  review_required: boolean;
  api_key_required: false;
};

export type CodexAnalysisRequest = {
  status: "prepared";
  request_path: string;
  request: {
    request_id: string;
    project_id: string;
    guide: string[];
    shots: unknown[];
    response_schema: unknown;
  };
};

export function getCodexAnalysisStatus(projectId: string): Promise<CodexAnalysisStatus> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/codex`, { timeoutMs: 120_000 });
}

export function prepareCodexAnalysis(projectId: string): Promise<CodexAnalysisRequest> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/codex/prepare`, {
    method: "POST", body: "{}", timeoutMs: 120_000
  });
}

export function applyCodexAnalysis(projectId: string, responseText: string): Promise<{
  status: "applied" | "incomplete";
  review_required: true;
  result: { summary: string; diagnostics: string[] };
}> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/codex/apply`, {
    method: "POST", body: responseText, timeoutMs: 120_000
  });
}

export async function validateIntake(
  source?: string
): Promise<{ ready: boolean; checks: Array<{ label: string; status: string; detail?: string }>; backend: true }> {
  const result = await request<{ ready: boolean; checks?: Array<{ label: string; status: string; detail?: string }> }>(
    "/api/intake/validate",
    { method: "POST", body: JSON.stringify({ source }) }
  );
  return { ready: Boolean(result.ready), checks: result.checks ?? [], backend: true };
}

export async function createProjectFromIntake(
  source: string,
  profile = "research"
): Promise<{ project_id: string; backend: true; result?: Record<string, unknown> }> {
  const data = await request<{ project_id?: string; result?: Record<string, unknown> }>("/api/projects", {
    method: "POST",
    timeoutMs: 15 * 60 * 1000,
    body: JSON.stringify({
      source,
      profile,
      language: "auto",
      delivery_language: "en",
      skip_asr: true
    })
  });
  if (!data.project_id) throw new ApiRequestError("The service did not return a project id.");
  return { project_id: data.project_id, result: data.result, backend: true };
}

export async function startAnalysisRun(source: string, profile = "research"): Promise<AnalysisRun> {
  return request<AnalysisRun>("/api/runs", {
    method: "POST",
    timeoutMs: 30_000,
    body: JSON.stringify({
      source,
      profile,
      language: "auto",
      delivery_language: "en",
      skip_asr: true,
      with_vision: false
    })
  });
}

export async function getAnalysisRun(runId: string): Promise<AnalysisRun> {
  return request<AnalysisRun>(`/api/runs/${encodeURIComponent(runId)}`, { timeoutMs: 15_000 });
}

export async function retryAnalysisRun(runId: string): Promise<AnalysisRun> {
  return request<AnalysisRun>(`/api/runs/${encodeURIComponent(runId)}/retry`, {
    method: "POST",
    timeoutMs: 30_000,
    body: "{}"
  });
}

export async function cancelAnalysisRun(runId: string): Promise<AnalysisRun> {
  return request<AnalysisRun>(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    timeoutMs: 30_000,
    body: "{}"
  });
}

export async function getRuntimeSettings(): Promise<{ settings: RuntimeSettings; backend: true }> {
  return { settings: await request<RuntimeSettings>("/api/settings/runtime"), backend: true };
}

export async function getDoctor(): Promise<{ doctor: DoctorPayload; backend: true }> {
  return { doctor: await request<DoctorPayload>("/api/runtime/doctor", { timeoutMs: 10_000 }), backend: true };
}
