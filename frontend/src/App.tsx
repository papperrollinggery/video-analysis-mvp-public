import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Database,
  Download,
  FileJson2,
  FileText,
  Film,
  FolderOpen,
  Headphones,
  ListVideo,
  Menu,
  PackageOpen,
  PencilLine,
  Plus,
  RefreshCw,
  Save,
  Settings,
  ShieldCheck,
  TerminalSquare,
  Video,
  X
} from "lucide-react";
import {
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams
} from "react-router-dom";
import {
  ApiRequestError,
  assetUrl,
  cancelAnalysisRun,
  getAnalysisRun,
  getDeliverables,
  getDoctor,
  getRuntimeSettings,
  listProjects,
  loadWorkspace,
  readableError,
  regenerateProjectReport,
  retryAnalysisRun,
  startAnalysisRun,
  updateShotReview,
  validateIntake
} from "./api/client";
import type {
  AnalysisRun,
  AudioReviewResult,
  DeliverableArtifact,
  DeliverablesPayload,
  DoctorPayload,
  ProjectSummary,
  RuntimeSettings,
  ShotBoundary,
  ShotReviewFields,
  WorkspaceBundle
} from "./types";
import { CodexAnalysisPanel } from "./components/CodexAnalysisPanel";
import { AudioReviewPanel } from "./features/audio/AudioReviewPanel";
import { ExportCenter } from "./features/exports/ExportCenter";

type MobileMode = "video" | "shots" | "audio" | "evidence" | "export";
type Drawer = "source" | "codex" | "review" | null;

const drawerFocusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "[tabindex]:not([tabindex='-1'])"
].join(",");

export function App() {
  return (
    <Routes>
      <Route path="/" element={<ProjectsPage />} />
      <Route path="/runs/:runId" element={<RunPage />} />
      <Route path="/projects/:projectId" element={<WorkspacePage />} />
      <Route path="/projects/:projectId/deliverables" element={<DeliverablesPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function AppShell({ children, projectId }: { children: ReactNode; projectId?: string }) {
  const workspacePath = projectId ? `/projects/${encodeURIComponent(projectId)}` : "";
  const deliverablesPath = projectId ? `${workspacePath}/deliverables` : "";
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="global-nav" aria-label="Primary navigation">
        <Link to="/" className="brand-mark" aria-label="Video Evidence Workbench home">VEW</Link>
        <nav>
          <NavLink to="/" end><FolderOpen aria-hidden="true" /><span>Projects</span></NavLink>
          {projectId ? (
            <NavLink to={workspacePath} end><Film aria-hidden="true" /><span>Shots</span></NavLink>
          ) : (
            <span className="nav-item is-disabled" aria-disabled="true" aria-label="Shots unavailable until a project is selected"><Film aria-hidden="true" /><span>Shots</span></span>
          )}
          {projectId ? (
            <NavLink to={deliverablesPath}><PackageOpen aria-hidden="true" /><span>Deliverables</span></NavLink>
          ) : (
            <span className="nav-item is-disabled" aria-disabled="true" aria-label="Deliverables unavailable until a project is selected"><PackageOpen aria-hidden="true" /><span>Deliverables</span></span>
          )}
          <NavLink to="/settings"><Settings aria-hidden="true" /><span>Settings</span></NavLink>
        </nav>
        <div className="about-link" aria-label="Local-first workspace">
          <ShieldCheck aria-hidden="true" /><span>Local-first</span>
        </div>
      </aside>
      <main id="main-content" className="app-main">{children}</main>
    </div>
  );
}

function ProjectsPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [source, setSource] = useState("");
  const [profile, setProfile] = useState("research");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [intakeChecks, setIntakeChecks] = useState<Array<{ label: string; status: string; detail?: string }>>([]);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      setProjects((await listProjects()).projects);
    } catch (requestError) {
      setError(readableError(requestError));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = source.trim();
    if (!trimmed) return;
    setCreating(true);
    setError("");
    setIntakeChecks([]);
    try {
      const validation = await validateIntake(trimmed);
      setIntakeChecks(validation.checks);
      if (!validation.ready) throw new Error("The source did not pass the local intake checks.");
      const run = await startAnalysisRun(trimmed, profile);
      navigate(`/runs/${encodeURIComponent(run.run_id)}`);
    } catch (requestError) {
      setError(readableError(requestError));
    } finally {
      setCreating(false);
    }
  }

  return (
    <AppShell>
      <div className="projects-page">
        <header className="page-header">
          <div>
            <p className="product-label">Video Evidence Workbench</p>
            <h1>Turn a video into evidence you can inspect.</h1>
            <p>Local shot boundaries, keyframes, review metadata, and portable research artifacts—without hiding API failures behind demo data.</p>
          </div>
          <button className="secondary-button" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw aria-hidden="true" /> Refresh
          </button>
        </header>

        <section className="intake-panel" aria-labelledby="intake-title">
          <div>
            <h2 id="intake-title">New analysis</h2>
            <p>Use a local video path. Browser URL ingest is disabled; trusted operators can use the CLI for URL sources.</p>
          </div>
          <form onSubmit={submit}>
            <label htmlFor="video-source">Video source</label>
            <div className="input-action-row">
              <input
                id="video-source"
                value={source}
                onChange={(event) => setSource(event.target.value)}
                placeholder="/absolute/path/to/video.mp4"
                disabled={creating}
              />
              <select value={profile} onChange={(event) => setProfile(event.target.value)} disabled={creating} aria-label="Analysis profile">
                <option value="research">Research</option>
                <option value="ads">Ads</option>
                <option value="shortform">Short-form</option>
                <option value="streaming">Streaming</option>
                <option value="festival">Festival</option>
              </select>
              <button className="primary-button" disabled={creating || !source.trim()}>
                {creating ? <CircleDashed className="spin" aria-hidden="true" /> : <Plus aria-hidden="true" />}
                {creating ? "Starting…" : "Analyze video"}
              </button>
            </div>
          </form>
          {intakeChecks.length > 0 && (
            <ul className="inline-checks" aria-label="Intake checks">
              {intakeChecks.map((check) => {
                const passed = check.status === "ready";
                return (
                  <li key={check.label} className={passed ? "is-ready" : "is-blocked"}>
                    {passed ? <CheckCircle2 aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
                    <span><strong>{check.label}</strong>{check.detail && <small>{check.detail}</small>}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <ErrorNotice message={error} onRetry={() => void refresh()} />

        <section className="project-list-section" aria-labelledby="projects-title">
          <div className="section-heading">
            <div><h2 id="projects-title">Projects</h2><p>{projects.length} local project{projects.length === 1 ? "" : "s"}</p></div>
          </div>
          {loading ? <LoadingRows label="Loading local projects" /> : projects.length === 0 ? (
            <div className="empty-state"><FolderOpen aria-hidden="true" /><h3>No projects yet</h3><p>Analyze a video to create the first evidence package.</p></div>
          ) : (
            <div className="project-list">
              {projects.map((project) => {
                const ready = project.readiness?.status === "ready";
                return (
                  <Link key={project.project_id} to={`/projects/${encodeURIComponent(project.project_id)}`} className="project-row">
                    <span className="project-icon"><Video aria-hidden="true" /></span>
                    <span className="project-title"><strong>{humanizeId(project.project_id)}</strong><small>{project.source ?? "Local source"}</small></span>
                    <span className="project-metric"><strong>{project.media?.shot_count ?? "—"}</strong><small>shots</small></span>
                    <span className={`status-text ${ready ? "is-ready" : "is-review"}`}>
                      {ready ? <CheckCircle2 aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
                      {ready ? "Ready" : project.readiness?.status ?? project.status ?? "Not verified"}
                    </span>
                    <ChevronRight aria-hidden="true" />
                  </Link>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </AppShell>
  );
}

const runStageLabels: Record<string, string> = {
  ingest: "Ingest & verify media",
  visual: "Detect shots & extract frames",
  audio: "Analyze audio & rhythm",
  report: "Build evidence package",
  finalize: "Verify workspace"
};

function newestRun(previous: AnalysisRun | null, incoming: AnalysisRun): AnalysisRun {
  if (!previous) return incoming;
  return Date.parse(incoming.updated_at) >= Date.parse(previous.updated_at) ? incoming : previous;
}

function RunPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<AnalysisRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [pollGeneration, setPollGeneration] = useState(0);
  const [error, setError] = useState("");
  const terminal = run?.state === "completed" || run?.state === "failed" || run?.state === "interrupted" || run?.state === "cancelled";

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const current = await getAnalysisRun(runId);
        if (disposed) return;
        setRun((previous) => newestRun(previous, current));
        setError("");
        setLoading(false);
        if (!(["completed", "failed", "interrupted", "cancelled"] as string[]).includes(current.state)) {
          timer = window.setTimeout(() => void poll(), 750);
        }
      } catch (requestError) {
        if (disposed) return;
        setError(readableError(requestError));
        setLoading(false);
        const status = requestError instanceof ApiRequestError ? requestError.status : undefined;
        if (status === undefined || status === 408 || status === 429 || status >= 500) {
          timer = window.setTimeout(() => void poll(), 2_000);
        }
      }
    }
    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId, pollGeneration]);

  async function retry() {
    setRetrying(true);
    setError("");
    try {
      const current = await retryAnalysisRun(runId);
      setRun((previous) => newestRun(previous, current));
      setPollGeneration((generation) => generation + 1);
    } catch (requestError) {
      setError(readableError(requestError));
    } finally {
      setRetrying(false);
    }
  }

  async function cancel() {
    setCancelling(true);
    setError("");
    try {
      const current = await cancelAnalysisRun(runId);
      setRun((previous) => newestRun(previous, current));
      setPollGeneration((generation) => generation + 1);
    } catch (requestError) {
      setError(readableError(requestError));
    } finally {
      setCancelling(false);
    }
  }

  const stages = ["ingest", "visual", "audio", "report", "finalize"].map((id) => {
    const history = run?.stages.filter((stage) => stage.id === id) ?? [];
    const latest = run?.state === "queued"
      ? undefined
      : history.filter((stage) => stage.attempt === run?.attempt).at(-1);
    return { id, latest };
  });
  const statusLabel = run?.state === "completed"
    ? "Analysis complete"
    : run?.state === "failed"
      ? "Analysis failed"
    : run?.state === "interrupted"
      ? "Worker interrupted"
      : run?.state === "cancelled"
        ? "Analysis cancelled"
        : run?.state === "cancelling"
          ? "Stopping safely"
        : run?.state === "queued"
          ? "Queued"
          : `Running ${humanizeId(run?.stage ?? "analysis")}`;

  return (
    <AppShell>
      <div className="run-page">
        <header className="page-header compact">
          <div>
            <Link className="back-link" to="/"><ArrowLeft aria-hidden="true" /> Projects</Link>
            <p className="product-label">Persistent local run</p>
            <h1>{statusLabel}</h1>
            <p>This page is safe to close and reopen. Progress and failure receipts are stored in the local workspace.</p>
          </div>
          {run?.state === "completed" && (
            <Link className="primary-button" to={`/projects/${encodeURIComponent(run.project_id)}`}>
              Open evidence workspace <ArrowUpRight aria-hidden="true" />
            </Link>
          )}
          {(run?.state === "queued" || run?.state === "running") && (
            <button className="secondary-button" onClick={() => void cancel()} disabled={cancelling}>
              {cancelling ? <CircleDashed className="spin" aria-hidden="true" /> : <X aria-hidden="true" />}
              {cancelling ? "Requesting stop…" : "Cancel after this stage"}
            </button>
          )}
        </header>

        {loading && !run ? <LoadingRows label="Loading analysis run" /> : (
          <section className={`run-card is-${run?.state ?? "unknown"}`} aria-live="polite">
            <div className="run-summary">
              <span className="run-state-icon" aria-hidden="true">
                {run?.state === "completed" ? <CheckCircle2 /> : terminal ? <AlertTriangle /> : <CircleDashed className="spin" />}
              </span>
              <div>
                <strong>{statusLabel}</strong>
                <p>{run?.result?.summary ?? run?.error?.message ?? "The worker is preparing the next verified stage."}</p>
              </div>
              <span className="run-percent">{Math.round(run?.progress ?? 0)}%</span>
            </div>
            <div
              className="run-progress"
              role="progressbar"
              aria-label="Analysis progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(run?.progress ?? 0)}
            >
              <span style={{ width: `${Math.max(0, Math.min(100, run?.progress ?? 0))}%` }} />
            </div>

            <ol className="run-stages" aria-label="Analysis stages">
              {stages.map(({ id, latest }, index) => {
                const active = run?.stage === id && run?.state === "running";
                const state = latest?.state ?? (active ? "running" : "pending");
                return (
                  <li key={id} className={`is-${state}`}>
                    <span className="stage-index">{state === "completed" || state === "skipped" ? <Check /> : index + 1}</span>
                    <span><strong>{runStageLabels[id]}</strong><small>{latest?.detail ?? formatStageStatus(state, latest?.elapsed_seconds)}</small></span>
                  </li>
                );
              })}
            </ol>

            <dl className="run-receipt">
              <div><dt>Run ID</dt><dd>{run?.run_id ?? runId}</dd></div>
              <div><dt>Project</dt><dd>{run?.project_id ?? "Pending"}</dd></div>
              <div><dt>Attempt</dt><dd>{run?.attempt ?? 0}</dd></div>
              <div><dt>Updated</dt><dd>{formatRunTime(run?.updated_at)}</dd></div>
            </dl>

            {(run?.state === "failed" || run?.state === "interrupted" || run?.state === "cancelled") && (
              <div className="run-recovery" role="alert">
                <AlertTriangle aria-hidden="true" />
                <div><strong>{run.error?.type ?? "Run stopped"}</strong><p>{run.error?.message ?? "The local worker stopped before completion."}</p></div>
                <button className="primary-button" onClick={() => void retry()} disabled={retrying || run.error?.retriable === false}>
                  {retrying ? <CircleDashed className="spin" aria-hidden="true" /> : <RefreshCw aria-hidden="true" />}
                  {retrying ? "Restarting…" : "Resume safely"}
                </button>
              </div>
            )}
          </section>
        )}
        <ErrorNotice message={error} onRetry={() => window.location.reload()} />
      </div>
    </AppShell>
  );
}

function WorkspacePage() {
  const { projectId = "" } = useParams();
  const [bundle, setBundle] = useState<WorkspaceBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [mobileMode, setMobileMode] = useState<MobileMode>("shots");
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [regenerating, setRegenerating] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const drawerCloseRef = useRef<HTMLButtonElement>(null);
  const drawerOpenerRef = useRef<HTMLElement | null>(null);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const next = await loadWorkspace(projectId);
      setBundle(next);
      setSelectedId((current) => current || next.media.shot_boundaries?.[0]?.id || "");
    } catch (requestError) {
      setBundle(null);
      setError(readableError(requestError));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, [projectId]);

  useEffect(() => {
    if (!drawer) return;
    const background = document.querySelector<HTMLElement>(".app-shell");
    const dialog = drawerRef.current;
    const opener = drawerOpenerRef.current;
    const backgroundHadInert = background?.hasAttribute("inert") ?? false;
    const previousAriaHidden = background?.getAttribute("aria-hidden") ?? null;
    background?.setAttribute("inert", "");
    background?.setAttribute("aria-hidden", "true");

    const initialFocusFrame = window.requestAnimationFrame(() => drawerCloseRef.current?.focus());
    function handleDialogKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>(drawerFocusableSelector)]
        .filter((element) => element.getAttribute("aria-hidden") !== "true" && element.getClientRects().length > 0);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) {
        event.preventDefault();
        return;
      }
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
    dialog?.addEventListener("keydown", handleDialogKeyDown);
    return () => {
      window.cancelAnimationFrame(initialFocusFrame);
      dialog?.removeEventListener("keydown", handleDialogKeyDown);
      if (!backgroundHadInert) background?.removeAttribute("inert");
      if (previousAriaHidden === null) background?.removeAttribute("aria-hidden");
      else background?.setAttribute("aria-hidden", previousAriaHidden);
      window.requestAnimationFrame(() => opener?.focus());
    };
  }, [drawer]);

  const shots = bundle?.media.shot_boundaries ?? [];
  const selectedIndex = Math.max(0, shots.findIndex((shot) => shot.id === selectedId));
  const selected = shots[selectedIndex];
  const handoffArtifact = findArtifact(bundle?.deliverables.artifacts, "codex_handoff");
  const visualizationArtifact = findArtifact(bundle?.deliverables.artifacts, "visualization_dataset");

  function openDrawer(next: Exclude<Drawer, null>, opener: HTMLElement) {
    drawerOpenerRef.current = opener;
    setDrawer(next);
  }

  function closeDrawer() {
    setDrawer(null);
  }

  function selectShot(shot: ShotBoundary) {
    setSelectedId(shot.id);
    if (videoRef.current && typeof shot.start_time === "number") videoRef.current.currentTime = shot.start_time;
  }

  function acceptReviewedWorkspace(next: WorkspaceBundle, advance: boolean): boolean {
    setBundle(next);
    const nextShots = next.media.shot_boundaries ?? [];
    if (!advance || nextShots.length === 0) return false;
    const currentIndex = Math.max(0, nextShots.findIndex((shot) => shot.id === selectedId));
    const ordered = [...nextShots.slice(currentIndex + 1), ...nextShots.slice(0, currentIndex + 1)];
    const unresolved = ordered.find(
      (shot) => shot.readiness_status !== "ready" || shot.annotation_verification === "unverified"
    );
    if (unresolved) {
      setSelectedId(unresolved.id);
      if (videoRef.current && typeof unresolved.start_time === "number") {
        videoRef.current.currentTime = unresolved.start_time;
      }
      return unresolved.id !== selectedId;
    }
    return false;
  }

  async function regenerate() {
    setRegenerating(true);
    setActionError("");
    setActionMessage("");
    try {
      const next = await regenerateProjectReport(projectId);
      setBundle(next);
      setActionMessage("Evidence package finalized from the current reviewed shots.");
    } catch (requestError) {
      setActionError(readableError(requestError));
    } finally {
      setRegenerating(false);
    }
  }

  async function acceptAudioReview(result: AudioReviewResult) {
    setActionError("");
    try {
      setBundle(await loadWorkspace(projectId));
      setActionMessage(result.changed
        ? "Audio review saved. Finalize remains a separate action; no client files were generated."
        : result.report_regeneration_required
          ? "This review already matched the event; the current report still requires Finalize. No client files were generated."
          : "This review already matched the finalized evidence. Nothing was regenerated or exported.");
    } catch (requestError) {
      setActionError(`Audio review was saved, but the workspace refresh failed: ${readableError(requestError)}`);
    }
  }

  if (loading) {
    return <AppShell projectId={projectId}><div className="full-page-state"><CircleDashed className="spin" /><h1>Loading verified project data</h1><p>No sample fallback will be shown.</p></div></AppShell>;
  }
  if (!bundle || error) {
    return <AppShell projectId={projectId}><div className="full-page-state is-error"><AlertTriangle /><h1>Project data unavailable</h1><p>{error}</p><button className="primary-button" onClick={() => void refresh()}><RefreshCw />Retry</button></div></AppShell>;
  }

  const readiness = bundle.project.readiness ?? bundle.deliverables.readiness;
  const reviewVideo = bundle.media.review_video;
  const ready = readiness?.status === "ready";
  const humanReview = readiness?.human_review_override === true;
  const visionAnnotation = readiness?.vision_annotation_complete === true;
  const boundaryReviewBound = typeof readiness?.boundary_review_binding === "object" && readiness.boundary_review_binding !== null;
  const audioTimelineAvailable = readiness?.audio_timeline_available;
  const audioReviewComplete = readiness?.audio_review_complete;
  const audioNeedsReview = readiness?.audio_requires_review_count ?? 0;
  const readinessReasons = authoritativeReadinessReasons(bundle, readiness);
  const readinessChecks = readiness?.checks ?? [];
  const reviewedShots = shots.filter((shot) => shot.annotation_verification === "human_reviewed").length;

  return (
    <AppShell projectId={projectId}>
      <article className="workspace" data-mobile-mode={mobileMode}>
        <header className="workspace-header">
          <div className="workspace-title">
            <Link to="/" className="mobile-back" aria-label="Back to projects"><ArrowLeft /></Link>
            <div><h1>{humanizeId(projectId)}</h1><p><Database aria-hidden="true" /> Local service data <span aria-hidden="true">·</span> {reviewVideo?.duration_seconds ? `${reviewVideo.duration_seconds.toFixed(2)}s` : "duration unavailable"}</p></div>
          </div>
          <div className="workspace-state">
            <span className={`status-text ${ready ? "is-ready" : "is-review"}`}>
              {ready ? <CheckCircle2 aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
              {ready ? (humanReview ? "Ready after human review" : "Ready") : readiness?.status ?? "Not verified"}
            </span>
            <button className="secondary-button desktop-action" onClick={(event) => openDrawer("source", event.currentTarget)}><Database /> Source & provenance</button>
            <button className="primary-button desktop-action" onClick={(event) => openDrawer("codex", event.currentTarget)}><TerminalSquare /> Open Codex analysis</button>
            <button className="icon-button mobile-menu" onClick={(event) => openDrawer("source", event.currentTarget)} aria-label="Open project details"><Menu /></button>
          </div>
        </header>

        <MobileModeNav value={mobileMode} onChange={setMobileMode} />

        <div className="workspace-grid">
          <section className="video-stage" aria-label="Video and shot timeline">
            <div className="video-frame">
              {reviewVideo?.url ? (
                <video ref={videoRef} src={assetUrl(reviewVideo.url)} controls preload="metadata" poster={assetUrl(primaryFrame(selected)?.url)}>
                  Your browser cannot play this review video.
                </video>
              ) : <div className="missing-media"><Video /><p>Review video is unavailable.</p></div>}
              <div className="video-meta"><span>{formatTime(selected?.start_time)} – {formatTime(selected?.end_time)}</span><span>{reviewVideo?.resolution ?? "Resolution unavailable"} · {reviewVideo?.frame_rate ? `${reviewVideo.frame_rate.toFixed(2)} fps` : "FPS unavailable"}</span></div>
            </div>
            <div className="shot-strip" aria-label={`${shots.length} shot boundaries`}>
              {shots.map((shot, index) => (
                <button
                  key={shot.id}
                  className={shot.id === selected?.id ? "is-selected" : ""}
                  onClick={() => selectShot(shot)}
                  aria-pressed={shot.id === selected?.id}
                  aria-label={`Shot ${shot.shot_no ?? index + 1}, ${formatRange(shot)}`}
                >
                  <span>{padShot(shot.shot_no ?? index + 1)}</span>
                  {primaryFrame(shot)?.url ? <img src={assetUrl(primaryFrame(shot)?.url)} alt="" /> : <span className="frame-placeholder" />}
                </button>
              ))}
            </div>
            <div className="timeline-scale" aria-hidden="true"><span>00:00</span><span>{formatTime((reviewVideo?.duration_seconds ?? 0) / 2)}</span><span>{formatTime(reviewVideo?.duration_seconds)}</span></div>
          </section>

          {selected && (
            <aside className="shot-inspector" aria-labelledby="selected-shot-title">
              <div className="inspector-heading">
                <div><p>Selected shot receipt</p><h2 id="selected-shot-title">Shot {padShot(selected.shot_no ?? selectedIndex + 1)}</h2></div>
                <span className={`status-text ${selected.annotation_verification === "human_reviewed" ? "is-ready" : selected.annotation_verification === "provider_receipt_verified" ? "is-receipt" : "is-review"}`}>
                  {selected.annotation_verification === "human_reviewed" ? <CheckCircle2 /> : selected.annotation_verification === "provider_receipt_verified" ? <ShieldCheck /> : <AlertTriangle />}
                  {formatAnnotationVerification(selected.annotation_verification)}
                </span>
              </div>
              <p className="large-timecode">{formatTime(selected.start_time)} – {formatTime(selected.end_time)}</p>
              <p className="muted">Measured timing {formatDuration(selected.duration)} · detected boundary confidence {selected.boundary_confidence ?? "unrated"}</p>
              <dl className="evidence-fields">
                <EvidenceField label="Annotation source" value={formatAnnotationSource(selected.annotation_source)} />
                <EvidenceField label="Verification" value={formatAnnotationVerification(selected.annotation_verification)} />
                <EvidenceField label="Per-shot readiness" value={selected.readiness_status ?? "Not reviewed"} />
                <EvidenceField label="Readiness reasons" value={formatReadinessReasons(selected.readiness_reasons)} />
                <EvidenceField label="Story beat" value={selected.story_beat} qualifier="interpretation" />
                <EvidenceField label="Shot scale" value={selected.shot_size} qualifier="visual annotation" />
                <EvidenceField label="Camera angle" value={selected.angle} qualifier="visual annotation" />
                <EvidenceField label="Visible action" value={selected.visual_content} qualifier="visual annotation" />
                <EvidenceField label="Audio note" value={selected.sound} qualifier="annotation" />
                <EvidenceField label="Meaning" value={selected.meaning} qualifier="interpretation" />
                <EvidenceField label="Visual confidence" value={formatConfidence(selected.visual_confidence)} qualifier="annotation score" />
              </dl>
              <button className="secondary-button inspector-review-action" onClick={(event) => openDrawer("review", event.currentTarget)}>
                <PencilLine aria-hidden="true" /> Review this shot
              </button>
              {selected.annotation_verification === "provider_receipt_verified" && (
                <p className="verification-boundary"><ShieldCheck aria-hidden="true" /> Provider receipt verified confirms bound provider provenance; it is not factual human review.</p>
              )}
              <details className="evidence-files">
                <summary><FileText /> Evidence files · {selected.keyframes?.filter((item) => item.present).length ?? 0}</summary>
                <ul>{selected.keyframes?.filter((item) => item.present && item.url).map((frame) => (
                  <li key={frame.relative_path}><a href={assetUrl(frame.url)} target="_blank" rel="noreferrer"><FileJson2 />{shortPath(frame.relative_path)}<ArrowUpRight /></a></li>
                ))}</ul>
              </details>
            </aside>
          )}
        </div>

        <AudioReviewPanel
          projectId={projectId}
          durationSeconds={reviewVideo?.duration_seconds ?? 0}
          selectedShotId={selected?.id}
          onSeek={(seconds) => { if (videoRef.current) videoRef.current.currentTime = seconds; }}
          onCue={(seconds) => {
            if (videoRef.current) videoRef.current.currentTime = seconds;
            setMobileMode("video");
            setActionMessage(`Video cued to ${formatTime(seconds)}. Playback remains under operator control.`);
            window.requestAnimationFrame(() => videoRef.current?.focus());
          }}
          onReviewSaved={acceptAudioReview}
        />

        <section className="shot-table-section" aria-labelledby="shot-table-title">
          <div className="section-heading"><div><h2 id="shot-table-title">Shot boundaries & annotations</h2><p>{shots.length} boundaries with per-shot provenance receipts</p></div><span className="source-receipt"><Database /> API receipt · live</span></div>
          <div className="table-scroll"><table>
            <thead><tr><th>#</th><th>Frame</th><th>In</th><th>Out</th><th>Duration</th><th>Beat (interpretation)</th><th>Visual annotation</th><th>Annotation confidence</th><th>Annotation source</th><th>Verification</th><th>Readiness</th></tr></thead>
            <tbody>{shots.map((shot, index) => (
              <tr key={shot.id} className={shot.id === selected?.id ? "is-selected" : ""}>
                <td><button className="table-shot-button" onClick={() => selectShot(shot)}>{padShot(shot.shot_no ?? index + 1)}</button></td>
                <td>{primaryFrame(shot)?.url ? <img src={assetUrl(primaryFrame(shot)?.url)} alt={`Representative frame for shot ${shot.shot_no ?? index + 1}`} /> : "—"}</td>
                <td className="mono">{formatTime(shot.start_time)}</td><td className="mono">{formatTime(shot.end_time)}</td>
                <td className="mono">{formatDuration(shot.duration)}</td><td>{shot.story_beat ?? "—"}</td><td>{shot.visual_content ?? "Not annotated"}</td>
                <td>{formatConfidence(shot.visual_confidence)}</td><td>{formatAnnotationSource(shot.annotation_source)}</td><td>{formatAnnotationVerification(shot.annotation_verification)}</td><td>{shot.readiness_status ?? "Not reviewed"}</td>
              </tr>
            ))}</tbody>
          </table></div>
        </section>

        <section className="readiness-strip" aria-label="Readiness evidence">
          <span className="readiness-title">Readiness</span>
          <ReadinessItem ok={Boolean(reviewVideo?.present)} label={reviewVideo?.present ? "Media file present" : "Media file missing"} />
          <ReadinessItem ok={shots.length > 0} label={`${shots.length} boundaries`} />
          <ReadinessItem ok={visionAnnotation || humanReview} label={visionAnnotation ? "Provider receipts complete · not human review" : humanReview ? "Human review complete" : `${reviewedShots}/${shots.length} human reviewed`} warning />
          <ReadinessItem
            ok={readiness?.boundary_review_complete === true}
            label={readiness?.boundary_review_complete === true ? (boundaryReviewBound ? "Low-confidence cuts reviewed" : "No low-confidence cuts") : "Boundary review required"}
            warning
          />
          <ReadinessItem
            ok={audioReviewComplete === true}
            warning={audioTimelineAvailable !== true}
            label={audioTimelineAvailable == null
              ? "Audio review status not reported"
              : audioTimelineAvailable === false
                ? "Audio timeline unavailable · unknown, not silence"
                : audioReviewComplete === true
                  ? `${readiness?.audio_event_count ?? 0} audio events resolved`
                  : `${audioNeedsReview} audio event${audioNeedsReview === 1 ? "" : "s"} require review`}
          />
          <div className="readiness-actions">
            <button className="primary-button" onClick={(event) => openDrawer("review", event.currentTarget)} disabled={!selected}><PencilLine /> Review selected</button>
            <button className="secondary-button" onClick={() => void regenerate()} disabled={regenerating}><RefreshCw className={regenerating ? "spin" : ""} />{regenerating ? "Finalizing…" : "Finalize package"}</button>
            <Link className="secondary-button" to={`/projects/${encodeURIComponent(projectId)}/deliverables`}><PackageOpen /> Deliverables</Link>
          </div>
        </section>

        {(actionError || actionMessage) && (
          <div className={`workspace-action-status ${actionError ? "is-error" : "is-success"}`} role={actionError ? "alert" : "status"} aria-live="polite">
            {actionError ? <AlertTriangle /> : <CheckCircle2 />}
            <span>{actionError || actionMessage}</span>
          </div>
        )}

        {!ready && (
          <section className="gate-reasons" aria-labelledby="gate-reasons-title">
            <div className="gate-reasons-heading"><AlertTriangle aria-hidden="true" /><div><h2 id="gate-reasons-title">Export blocked by the current readiness gate</h2><p>Authoritative reasons and checks returned by the local API.</p></div></div>
            {readinessReasons.length > 0 ? <ul>{readinessReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p className="small-note">The API did not return a reason list. Inspect the readiness artifact before export.</p>}
            {readinessChecks.length > 0 && <dl className="gate-checks">{readinessChecks.map((check, index) => <div key={check.id ?? `${check.label}-${index}`}><dt>{check.label ?? check.id ?? `Check ${index + 1}`}</dt><dd>{check.status ?? "Not reported"}{check.message ? ` · ${check.message}` : ""}</dd></div>)}</dl>}
          </section>
        )}

        <section className="mobile-evidence-panel" aria-label="Evidence and provenance">
          <SourcePanel bundle={bundle} />
        </section>
        <section className="mobile-export-panel" aria-label="Deliverables">
          <ExportCenter
            projectId={projectId}
            allowed={readiness?.professional_export_allowed === true}
            blockedReasons={readinessReasons}
            compact
            onChanged={async () => setBundle(await loadWorkspace(projectId))}
          />
          <DeliverableList payload={bundle.deliverables} compact />
        </section>

        <div className="mobile-action-bar">
          <button className="primary-button" onClick={(event) => openDrawer("review", event.currentTarget)} disabled={!selected}><PencilLine /> Review</button>
          <Link className="secondary-button" to={`/projects/${encodeURIComponent(projectId)}/deliverables`}><PackageOpen /> Deliverables</Link>
          <button className="secondary-button" onClick={(event) => openDrawer("codex", event.currentTarget)}><TerminalSquare /> Codex</button>
        </div>

        {drawer && createPortal(
          <div className="drawer-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) closeDrawer(); }}>
            <aside ref={drawerRef} className="side-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
              <div className="drawer-heading"><div><p>{drawer === "review" ? "Human verification" : "Project receipt"}</p><h2 id="drawer-title">{drawer === "source" ? "Source & provenance" : drawer === "review" ? `Review shot ${padShot(selected?.shot_no ?? selectedIndex + 1)}` : "Codex analysis"}</h2></div><button ref={drawerCloseRef} className="icon-button" onClick={closeDrawer} aria-label="Close"><X /></button></div>
              {drawer === "source" ? <SourcePanel bundle={bundle} /> : drawer === "review" && selected ? (
                <ShotReviewForm
                  key={selected.id}
                  projectId={projectId}
                  shot={selected}
                  onWorkspace={acceptReviewedWorkspace}
                />
              ) : (
                <CodexAnalysisPanel projectId={projectId} onApplied={async () => setBundle(await loadWorkspace(projectId))}>
                  <div className="artifact-actions">
                    <ArtifactLink artifact={handoffArtifact} fallbackLabel="Codex handoff unavailable until Finalize" />
                    <ArtifactLink artifact={visualizationArtifact} fallbackLabel="Visualization dataset unavailable until Finalize" />
                  </div>
                  <p className="small-note"><strong>@Visualize:</strong> when available in ChatGPT web or desktop, it can preview a snapshot from the dataset. Availability depends on plan, platform, and account; it is not a live dashboard or a Codex CLI/IDE feature.</p>
                </CodexAnalysisPanel>
              )}
            </aside>
          </div>,
          document.body
        )}
      </article>
    </AppShell>
  );
}

function ShotReviewForm({
  projectId,
  shot,
  onWorkspace
}: {
  projectId: string;
  shot: ShotBoundary;
  onWorkspace: (workspace: WorkspaceBundle, advance: boolean) => boolean;
}) {
  const original = shot.review_fields ?? {};
  const [draft, setDraft] = useState<ShotReviewFields>({ ...original });
  const [decision, setDecision] = useState<"blocked" | "ready" | "rejected">(
    original.readiness_status ?? "blocked"
  );
  const [boundaryReviewed, setBoundaryReviewed] = useState(original.boundary_reviewed === true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const boundaryNeedsReview = (shot.boundary_confidence ?? "low").trim().toLowerCase() === "low";

  function textValue(field: keyof ShotReviewFields) {
    const value = draft[field];
    return typeof value === "string" ? value : "";
  }

  function updateText(field: keyof ShotReviewFields, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function save(advance: boolean) {
    const confidence = Number(draft.visual_confidence ?? 0);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      setError("Visual confidence must be a number from 0 to 1.");
      return;
    }
    if (decision === "ready" && boundaryNeedsReview && !boundaryReviewed) {
      setError("Confirm the detected boundary against the video and timecode before marking this low-confidence shot ready.");
      return;
    }
    setSaving(true);
    setError("");
    setMessage("Saving the human review…");
    const review: ShotReviewFields = {
      ...draft,
      visual_confidence: confidence,
      readiness_status: decision
    };
    if (boundaryNeedsReview) review.boundary_reviewed = boundaryReviewed;
    else delete review.boundary_reviewed;
    try {
      await updateShotReview(projectId, shot.id, shot.edit_version, review);
      setMessage("Review saved. Refreshing the current readiness receipt…");
      try {
        const next = await loadWorkspace(projectId);
        const advanced = onWorkspace(next, advance);
        const currentShot = next.media.shot_boundaries?.find((item) => item.id === shot.id);
        if (currentShot?.readiness_status === "ready") {
          setMessage(advanced ? "Review saved. Moved to the next unresolved shot." : "Review saved. No unresolved shot remains; finalize the package when review is complete.");
        } else {
          setMessage("Review saved. Resolve the remaining per-shot reasons before finalizing.");
        }
      } catch (refreshError) {
        setMessage("");
        setError(`Review saved, but the workspace refresh failed: ${readableError(refreshError)}`);
      }
    } catch (requestError) {
      setMessage("");
      setError(readableError(requestError));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="review-form" onSubmit={(event) => { event.preventDefault(); void save(false); }}>
      <div className="boundary-note">
        <ShieldCheck aria-hidden="true" />
        <div><strong>Human assertion, not automatic truth</strong><p>Check the source frames and timecode. Saving replaces this shot's annotation source with a local operator assertion. Finalize once after reviewing all shots.</p></div>
      </div>
      {shot.annotation_verification === "provider_receipt_verified" && (
        <p className="review-warning"><AlertTriangle aria-hidden="true" /> Editing this shot replaces its provider-verified annotation state with human review.</p>
      )}
      <label htmlFor={`content-${shot.id}`}>Observed visual content</label>
      <textarea id={`content-${shot.id}`} rows={4} value={textValue("content_summary")} onChange={(event) => updateText("content_summary", event.target.value)} disabled={saving} />
      <div className="review-field-grid">
        <div><label htmlFor={`subject-${shot.id}`}>Subject</label><input id={`subject-${shot.id}`} value={textValue("subject")} onChange={(event) => updateText("subject", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`action-${shot.id}`}>Action</label><input id={`action-${shot.id}`} value={textValue("action")} onChange={(event) => updateText("action", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`scale-${shot.id}`}>Shot scale</label><input id={`scale-${shot.id}`} value={textValue("shot_scale")} onChange={(event) => updateText("shot_scale", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`angle-${shot.id}`}>Camera angle</label><input id={`angle-${shot.id}`} value={textValue("camera_angle")} onChange={(event) => updateText("camera_angle", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`motion-${shot.id}`}>Camera motion</label><input id={`motion-${shot.id}`} value={textValue("camera_motion")} onChange={(event) => updateText("camera_motion", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`composition-${shot.id}`}>Composition</label><input id={`composition-${shot.id}`} value={textValue("composition")} onChange={(event) => updateText("composition", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`beat-${shot.id}`}>Story beat</label><input id={`beat-${shot.id}`} value={textValue("story_beat")} onChange={(event) => updateText("story_beat", event.target.value)} disabled={saving} /></div>
        <div><label htmlFor={`confidence-${shot.id}`}>Operator confidence (0–1)</label><input id={`confidence-${shot.id}`} inputMode="decimal" value={String(draft.visual_confidence ?? 0)} onChange={(event) => setDraft((current) => ({ ...current, visual_confidence: Number(event.target.value) }))} disabled={saving} /></div>
      </div>
      <label htmlFor={`text-${shot.id}`}>On-screen text</label>
      <textarea id={`text-${shot.id}`} rows={2} value={textValue("onscreen_text")} onChange={(event) => updateText("onscreen_text", event.target.value)} disabled={saving} />
      <label htmlFor={`dialogue-${shot.id}`}>Dialogue</label>
      <textarea id={`dialogue-${shot.id}`} rows={2} value={textValue("dialogue")} onChange={(event) => updateText("dialogue", event.target.value)} disabled={saving} />
      <label htmlFor={`notes-${shot.id}`}>Review notes</label>
      <textarea id={`notes-${shot.id}`} rows={2} value={textValue("review_notes")} onChange={(event) => updateText("review_notes", event.target.value)} disabled={saving} />

      {boundaryNeedsReview && (
        <label className="review-assertion">
          <input type="checkbox" checked={boundaryReviewed} onChange={(event) => setBoundaryReviewed(event.target.checked)} disabled={saving} />
          <span><strong>I checked this low-confidence boundary.</strong><small>The assertion is stored separately and bound to the current visual-generation receipt; detector confidence remains unchanged.</small></span>
        </label>
      )}

      <fieldset className="review-decision">
        <legend>Review decision</legend>
        <label><input type="radio" name={`decision-${shot.id}`} checked={decision === "blocked"} onChange={() => setDecision("blocked")} disabled={saving} /><span><strong>Keep blocked</strong><small>Save progress without permitting professional export.</small></span></label>
        <label><input type="radio" name={`decision-${shot.id}`} checked={decision === "ready"} onChange={() => setDecision("ready")} disabled={saving} /><span><strong>Reviewed and ready</strong><small>I checked these fields against the source evidence.</small></span></label>
        <label><input type="radio" name={`decision-${shot.id}`} checked={decision === "rejected"} onChange={() => setDecision("rejected")} disabled={saving} /><span><strong>Reject shot</strong><small>Keep the package blocked and record that this shot needs rework.</small></span></label>
      </fieldset>

      <div className="review-feedback" aria-live="polite">
        {error && <p className="is-error" role="alert"><AlertTriangle />{error}</p>}
        {message && <p className="is-success"><CheckCircle2 />{message}</p>}
      </div>
      <div className="review-actions">
        <button type="submit" className="secondary-button" disabled={saving}><Save />{saving ? "Saving…" : "Save review"}</button>
        <button type="button" className="primary-button" onClick={() => void save(true)} disabled={saving}><Save />Save & next unresolved</button>
      </div>
      <p className="small-note">The <a href={`/legacy/projects/${encodeURIComponent(projectId)}`}>legacy evidence viewer</a> remains available for read-only shot inspection during this pre-1.0 transition.</p>
    </form>
  );
}

function DeliverablesPage() {
  const { projectId = "" } = useParams();
  const [payload, setPayload] = useState<DeliverablesPayload | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh(showLoading = true) {
    if (showLoading) setLoading(true);
    setError("");
    try { setPayload((await getDeliverables(projectId)).deliverables); }
    catch (requestError) { setError(readableError(requestError)); }
    finally { if (showLoading) setLoading(false); }
  }
  useEffect(() => { void refresh(true); }, [projectId]);

  return (
    <AppShell projectId={projectId}>
      <div className="deliverables-page">
        <header className="page-header compact">
          <div><Link className="back-link" to={`/projects/${encodeURIComponent(projectId)}`}><ArrowLeft /> Back to shots</Link><h1>Evidence deliverables</h1><p>{humanizeId(projectId)} · only files confirmed by the local API are listed.</p></div>
          <button className="secondary-button" onClick={() => void refresh(true)} disabled={loading}><RefreshCw /> Refresh</button>
        </header>
        <ErrorNotice message={error} onRetry={() => void refresh(true)} />
        {loading ? <LoadingRows label="Loading deliverables" /> : payload ? <>
          <ExportCenter
            projectId={projectId}
            allowed={payload.readiness?.professional_export_allowed === true}
            blockedReasons={payload.export?.blocked_reasons}
            onChanged={() => refresh(false)}
          />
          <DeliverableList payload={payload} />
        </> : null}
      </div>
    </AppShell>
  );
}

function DeliverableList({ payload, compact = false }: { payload: DeliverablesPayload; compact?: boolean }) {
  const groups = useMemo(() => {
    const map = new Map<string, DeliverableArtifact[]>();
    for (const artifact of payload.artifacts ?? []) {
      const group = artifact.group ?? "other";
      map.set(group, [...(map.get(group) ?? []), artifact]);
    }
    return [...map.entries()];
  }, [payload.artifacts]);

  return (
    <div className={`deliverable-groups ${compact ? "is-compact" : ""}`}>
      {!compact && <div className="delivery-receipt"><div><span>Readiness</span><strong>{payload.readiness?.status ?? "Not verified"}</strong></div><div><span>Artifacts</span><strong>{payload.artifacts?.length ?? 0}</strong></div><div><span>Professional export</span><strong>{payload.readiness?.professional_export_allowed ? "Allowed" : "Blocked"}</strong></div></div>}
      {groups.map(([group, artifacts]) => (
        <section key={group} className="deliverable-group"><div className="section-heading"><div><h2>{humanizeId(group)}</h2><p>{artifacts.length} files</p></div></div>
          <div className="artifact-list">{artifacts.map((artifact) => {
            const openBlocked = artifact.readiness_status === "blocked";
            const missing = !artifact.present || artifact.readiness_status === "missing";
            const available = artifact.present && artifact.readiness_status === "available";
            const ready = artifact.present && artifact.readiness_status === "ready";
            const canOpen = Boolean(artifact.present && artifact.url && !openBlocked);
            const statusTone = available ? "is-ready" : ready ? "is-ready" : openBlocked || missing ? "is-blocked" : "is-review";
            const statusLabel = missing ? "Missing" : humanizeId(artifact.readiness_status ?? "present");
            const disabledReason = openBlocked ? "blocked by the current readiness gate" : missing ? "file is missing" : "file URL is unavailable";
            const disabledActionLabel = `Open ${artifact.label}: ${disabledReason}`;
            return (
              <div className="artifact-row" key={artifact.id}>
                <span className="artifact-icon">{artifact.content_type?.includes("json") ? <FileJson2 /> : <FileText />}</span>
                <span className="artifact-title"><strong>{artifact.label}</strong><small>{artifact.content_type ?? "Unknown type"} · {formatBytes(artifact.size_bytes)}{openBlocked ? " · Open blocked by readiness gate" : missing ? " · File missing" : ""}</small></span>
                <span className={`status-text ${statusTone}`}>{available || ready ? <CheckCircle2 /> : <AlertTriangle />}{statusLabel}</span>
                {canOpen ? (
                  <a className="icon-button" href={assetUrl(artifact.url!)} target="_blank" rel="noreferrer" aria-label={`Open ${artifact.label}`}><ArrowUpRight /></a>
                ) : (
                  <button type="button" className="icon-button is-disabled" disabled aria-disabled="true" aria-label={disabledActionLabel} title={disabledReason}><Download aria-hidden="true" /></button>
                )}
              </div>
            );
          })}</div>
        </section>
      ))}
    </div>
  );
}

function SettingsPage() {
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [doctor, setDoctor] = useState<DoctorPayload | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true); setError("");
    try {
      const [runtimeResult, doctorResult] = await Promise.all([getRuntimeSettings(), getDoctor()]);
      setSettings(runtimeResult.settings); setDoctor(doctorResult.doctor);
    } catch (requestError) { setError(readableError(requestError)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);

  const report = doctor?.doctor;
  return (
    <AppShell>
      <div className="settings-page">
        <header className="page-header compact"><div><h1>Local runtime</h1><p>Read-only configuration receipt from the service. Secrets are never rendered.</p></div><button className="secondary-button" onClick={() => void refresh()} disabled={loading}><RefreshCw /> Run check</button></header>
        <ErrorNotice message={error} onRetry={() => void refresh()} />
        {loading ? <LoadingRows label="Checking local runtime" /> : settings && (
          <div className="settings-layout">
            <section><div className="section-heading"><div><h2>Workspace</h2><p>Current local service scope</p></div></div><dl className="settings-list"><EvidenceField label="Workspace path" value={settings.workspace_path} /><EvidenceField label="Vision provider" value={settings.vision_provider} /><EvidenceField label="OpenAI model" value={settings.openai?.model} /><EvidenceField label="OpenAI key" value={settings.openai?.api_key_configured ? "Configured" : "Not configured"} /><EvidenceField label="MiniMax key" value={settings.minimax?.api_key_configured ? "Configured" : "Not configured"} /></dl></section>
            <section><div className="section-heading"><div><h2>Doctor</h2><p>Required local pipeline</p></div><span className={`status-text ${report?.status === "success" ? "is-ready" : "is-review"}`}>{report?.status === "success" ? <CheckCircle2 /> : <AlertTriangle />}{report?.status ?? "Unavailable"}</span></div><p className="doctor-summary">{report?.summary ?? "No doctor summary returned."}</p><ul className="next-actions">{report?.next_actions?.map((action) => <li key={action}><ChevronRight />{action}</li>)}</ul><div className="command-receipt"><TerminalSquare /><code>analyze-video doctor</code></div></section>
          </div>
        )}
      </div>
    </AppShell>
  );
}

function MobileModeNav({ value, onChange }: { value: MobileMode; onChange: (mode: MobileMode) => void }) {
  const items: Array<[MobileMode, ReactNode, string]> = [["video", <Video key="video" />, "Video"], ["shots", <ListVideo key="shots" />, "Shots"], ["audio", <Headphones key="audio" />, "Audio"], ["evidence", <Database key="evidence" />, "Evidence"], ["export", <PackageOpen key="export" />, "Export"]];
  return <nav className="mobile-mode-nav" aria-label="Workspace views">{items.map(([mode, icon, label]) => <button key={mode} className={value === mode ? "is-active" : ""} onClick={() => onChange(mode)} aria-pressed={value === mode}>{icon}<span>{label}</span></button>)}</nav>;
}

function SourcePanel({ bundle }: { bundle: WorkspaceBundle }) {
  const source = bundle.project.manifest?.source;
  const readiness = bundle.project.readiness ?? bundle.deliverables.readiness;
  const mediaBinding = readiness?.media_binding;
  const mediaBindingStatus = typeof mediaBinding === "object" && mediaBinding !== null && "status" in mediaBinding
    ? String((mediaBinding as { status?: unknown }).status ?? "not reported")
    : "not reported";
  const projectManifest = findArtifact(bundle.deliverables.artifacts, "project_manifest");
  const mediaPackage = findArtifact(bundle.deliverables.artifacts, "media_package");
  const lineage = findArtifact(bundle.deliverables.artifacts, "lineage_json");
  const readinessArtifact = findArtifact(bundle.deliverables.artifacts, "readiness_json");
  const boundaryReview = findArtifact(bundle.deliverables.artifacts, "boundary_review_json");
  const reasons = authoritativeReadinessReasons(bundle, readiness);
  const professionalExport = readiness?.professional_export_allowed === true
    ? "Allowed"
    : readiness?.professional_export_allowed === false
      ? "Blocked"
      : "Not reported";
  return <div className="source-panel"><div className="boundary-note"><Database /><div><strong>Backend data only</strong><p>All visible records came from one local API snapshot. Errors are shown instead of silently substituting sample data.</p></div></div><dl className="settings-list"><EvidenceField label="Project" value={bundle.project.project_id} /><EvidenceField label="Snapshot" value={bundle.snapshot_id} /><EvidenceField label="Report generation" value={bundle.generation_id ?? "No committed generation"} /><EvidenceField label="Source" value={source} /><EvidenceField label="Readiness" value={readiness?.status ?? "Not reported"} /><EvidenceField label="Professional export" value={professionalExport} /><EvidenceField label="Media file" value={bundle.media.review_video?.present ? "Present" : "Missing"} /><EvidenceField label="Media binding" value={mediaBindingStatus === "bound" ? "Bound to current receipt" : `Not verified (${mediaBindingStatus})`} /><EvidenceField label="Audio timeline" value={readiness?.audio_timeline_available === true ? "Bound to current audio intelligence" : "Unavailable · unknown, not silence"} /><EvidenceField label="Audio review" value={readiness?.audio_review_complete === true ? "Complete" : readiness?.audio_timeline_available === false ? "Not applicable until a timeline exists" : `${readiness?.audio_requires_review_count ?? 0} event(s) require review`} /><EvidenceField label="Readiness reasons" value={reasons.length ? reasons.join("; ") : "No reasons reported"} /><EvidenceField label="Canvas version" value={bundle.canvas.version} /><EvidenceField label="Canvas graph" value={`${bundle.canvas.nodes.length} nodes · ${bundle.canvas.edges.length} edges`} /><EvidenceField label="Shot boundaries" value={String(bundle.media.shot_boundaries?.length ?? 0)} /><EvidenceField label="Artifacts" value={String(bundle.deliverables.artifacts?.length ?? 0)} /></dl><div className="provenance-actions" aria-label="Provenance artifacts"><h3>Provenance artifacts</h3><ArtifactLink artifact={projectManifest} fallbackLabel="Project manifest not generated" /><ArtifactLink artifact={mediaPackage} fallbackLabel="Media package not generated" /><ArtifactLink artifact={lineage} fallbackLabel="Lineage record not generated" /><ArtifactLink artifact={readinessArtifact} fallbackLabel="Readiness record not generated" />{boundaryReview && <ArtifactLink artifact={boundaryReview} fallbackLabel="Boundary review receipt not generated" />}</div><p className="small-note">Interpretive fields may come from deterministic heuristics, optional vision annotation, or human review. Use the readiness, boundary review, and lineage files to distinguish them.</p></div>;
}

function EvidenceField({ label, value, qualifier }: { label: string; value: unknown; qualifier?: string }) {
  const display = typeof value === "string" || typeof value === "number" ? String(value) : "Not available";
  return <div><dt>{label}{qualifier && <small> {qualifier}</small>}</dt><dd>{display || "Not available"}</dd></div>;
}

function ReadinessItem({ ok, label, warning = false }: { ok: boolean; label: string; warning?: boolean }) {
  return <span className={`readiness-item ${ok ? "is-ok" : warning ? "is-warning" : "is-blocked"}`}>{ok ? <CheckCircle2 /> : <AlertTriangle />}{label}</span>;
}

function ArtifactLink({ artifact, fallbackLabel }: { artifact?: DeliverableArtifact; fallbackLabel: string }) {
  if (!artifact?.present || !artifact.url) return <span className="artifact-link is-disabled" aria-disabled="true"><AlertTriangle />{fallbackLabel}</span>;
  return <a className="artifact-link" href={assetUrl(artifact.url)} target="_blank" rel="noreferrer"><FileText />{artifact.label}<ArrowUpRight /></a>;
}

function ErrorNotice({ message, onRetry }: { message: string; onRetry: () => void }) {
  if (!message) return null;
  return <div className="error-notice" role="alert"><AlertTriangle /><div><strong>Local service error</strong><p>{message}</p></div><button className="secondary-button" onClick={onRetry}><RefreshCw />Retry</button></div>;
}

function LoadingRows({ label }: { label: string }) {
  return <div className="loading-rows" role="status" aria-label={label}><span /><span /><span /></div>;
}

function formatStageStatus(state: string, elapsed?: number | null): string {
  if (state === "pending") return "Waiting";
  if (state === "running") return "In progress";
  const duration = typeof elapsed === "number" ? ` · ${elapsed.toFixed(2)}s` : "";
  return `${humanizeId(state)}${duration}`;
}

function formatRunTime(value?: string | null): string {
  if (!value) return "Not started";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function findArtifact(artifacts: DeliverableArtifact[] | undefined, idPart: string) {
  return artifacts?.find((artifact) => artifact.id.includes(idPart) || artifact.label.toLowerCase().includes(idPart.replace("_", " ")));
}

function authoritativeReadinessReasons(bundle: WorkspaceBundle, readiness: WorkspaceBundle["project"]["readiness"]) {
  const direct = readiness?.reasons?.filter((reason): reason is string => typeof reason === "string" && Boolean(reason.trim())) ?? [];
  if (direct.length) return direct;
  return bundle.deliverables.export?.blocked_reasons?.filter((reason): reason is string => typeof reason === "string" && Boolean(reason.trim())) ?? [];
}

function primaryFrame(shot?: ShotBoundary) {
  return shot?.keyframes?.find((frame) => frame.relative_path?.includes("_mid")) ?? shot?.keyframes?.[0];
}

function humanizeId(value: string) {
  return value.replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function padShot(value: number) { return String(value).padStart(2, "0"); }
function formatTime(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  const minutes = Math.floor(value / 60);
  const seconds = value - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`;
}
function formatRange(shot: ShotBoundary) { return `${formatTime(shot.start_time)} to ${formatTime(shot.end_time)}`; }
function formatDuration(value?: number) { return typeof value === "number" ? `${value.toFixed(2)}s` : "—"; }
function formatConfidence(value?: number) { return typeof value === "number" ? `${Math.round(value * 100)}%` : "Not scored"; }
function formatAnnotationSource(value?: string) {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return "Unknown";
  if (normalized === "human") return "Human annotation";
  if (normalized === "machine") return "Heuristic annotation";
  if (normalized === "codex") return "Current Codex task proposal";
  return `Provider annotation (${normalized})`;
}
function formatAnnotationVerification(value?: ShotBoundary["annotation_verification"]) {
  if (value === "provider_receipt_verified") return "Provider receipt verified";
  if (value === "agent_submission_bound") return "Submission bound · human review required";
  return value === "human_reviewed" ? "Human reviewed" : "Unverified";
}
function formatReadinessReasons(value?: string[]) {
  return value?.length ? value.join("; ") : "No per-shot reasons reported";
}
function formatBytes(value?: number) {
  if (typeof value !== "number") return "Size unavailable";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
function shortPath(path?: string) { return path?.split("/").slice(-2).join("/") ?? "Evidence file"; }
