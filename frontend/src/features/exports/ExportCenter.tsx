import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FileText,
  LoaderCircle,
  RefreshCw,
  Save,
  Trash2,
  X
} from "lucide-react";

import {
  cancelClientExport,
  assetUrl,
  deleteClientExport,
  generateClientExport,
  getClientExportState,
  getExportCenter,
  readableError,
  recoverClientExports,
  saveClientExport
} from "../../api/client";
import type { ExportCenterPayload, ExportFormat } from "../../types";
import "./exports.css";

type ExportSelection = "xlsx" | "pdf" | "both";
type BusyAction = "cancel" | "save" | "delete" | "recover" | null;

export function ExportCenter({
  projectId,
  allowed,
  blockedReasons = [],
  compact = false,
  onChanged
}: {
  projectId: string;
  allowed: boolean;
  blockedReasons?: string[];
  compact?: boolean;
  onChanged?: () => void | Promise<void>;
}) {
  const [payloadRecord, setPayloadRecord] = useState<{ projectId: string; value: ExportCenterPayload } | null>(null);
  const [liveState, setLiveState] = useState<{ projectId: string; value: ExportCenterPayload["state"] } | null>(null);
  const [selection, setSelection] = useState<ExportSelection>("both");
  const [language, setLanguage] = useState("bilingual");
  const [density, setDensity] = useState("client");
  const [subtitle, setSubtitle] = useState("");
  const [logoPath, setLogoPath] = useState("");
  const [accentColor, setAccentColor] = useState("#C13A24");
  const [versionId, setVersionId] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [generatingProjectId, setGeneratingProjectId] = useState<string | null>(null);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const currentProject = useRef(projectId);
  const refreshSequence = useRef(0);
  const generationInFlight = useRef(false);
  const mounted = useRef(true);
  currentProject.current = projectId;

  const payload = payloadRecord?.projectId === projectId ? payloadRecord.value : null;

  function isActiveProject(candidate: string): boolean {
    return mounted.current && currentProject.current === candidate;
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function refresh(showLoading = false) {
    if (showLoading) setLoading(true);
    const requestedProject = projectId;
    const sequence = ++refreshSequence.current;
    try {
      const value = await getExportCenter(requestedProject);
      if (isActiveProject(requestedProject) && refreshSequence.current === sequence) {
        setPayloadRecord({ projectId: requestedProject, value });
      }
    } catch (requestError) {
      if (isActiveProject(requestedProject) && refreshSequence.current === sequence) {
        setError(readableError(requestError));
      }
    } finally {
      if (showLoading && isActiveProject(requestedProject) && refreshSequence.current === sequence) {
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    refreshSequence.current += 1;
    setPayloadRecord(null);
    setLiveState(null);
    setConfirmDelete(null);
    setError("");
    setMessage("");
    setCancelRequested(false);
    void refresh(true);
  }, [projectId]);

  useEffect(() => {
    if (generatingProjectId !== projectId) return;
    let stopped = false;
    let timer: number | undefined;
    const requestedProject = projectId;

    async function poll() {
      try {
        const value = await getClientExportState(requestedProject);
        if (!stopped && isActiveProject(requestedProject)) {
          setLiveState({ projectId: requestedProject, value });
        }
      } catch {
        // The generation request remains authoritative. A transient progress
        // read must not turn a still-running export into a visible failure.
      } finally {
        if (!stopped) timer = window.setTimeout(() => void poll(), 1_200);
      }
    }

    void poll();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [generatingProjectId, projectId]);

  const formats = useMemo<ExportFormat[]>(
    () => selection === "both" ? ["xlsx", "pdf"] : [selection],
    [selection]
  );
  const state = liveState?.projectId === projectId ? liveState.value : payload?.state;
  const current = payload?.current;
  const currentIsUsable = current?.lifecycle_state === "current";
  const generating = generatingProjectId === projectId;
  const anotherProjectGenerating = generatingProjectId !== null && !generating;
  const rendering = state?.status === "rendering";
  const publishing = state?.status === "publishing";
  const actionLocked = loading || !payload || generatingProjectId !== null || busy !== null || rendering || publishing;

  async function changed(operationProject: string) {
    if (!isActiveProject(operationProject)) return;
    await refresh(false);
    if (isActiveProject(operationProject)) await onChanged?.();
  }

  async function generate() {
    if (generationInFlight.current) return;
    const operationProject = projectId;
    generationInFlight.current = true;
    setGeneratingProjectId(operationProject);
    setCancelRequested(false);
    setError("");
    setMessage("");
    const key = `ui-${Date.now()}-${crypto.randomUUID().slice(0, 12)}`;
    try {
      const settings: Record<string, string> = { language, density, accent_color: accentColor };
      if (subtitle.trim()) settings.project_subtitle = subtitle.trim();
      if (logoPath.trim()) settings.logo_path = logoPath.trim();
      const receipt = await generateClientExport(operationProject, {
        formats,
        settings,
        idempotency_key: key
      });
      if (isActiveProject(operationProject)) {
        setMessage(`Current package generated · ${receipt.formats.map((item) => item.toUpperCase()).join(" + ")}.`);
        await changed(operationProject);
      }
    } catch (requestError) {
      if (isActiveProject(operationProject)) {
        setError(readableError(requestError));
        await refresh(false);
      }
    } finally {
      generationInFlight.current = false;
      if (mounted.current) {
        setGeneratingProjectId(null);
        setLiveState(null);
        setCancelRequested(false);
      }
    }
  }

  async function cancel() {
    if (!state?.request_digest) return;
    const operationProject = projectId;
    setBusy("cancel");
    setError("");
    try {
      const result = await cancelClientExport(operationProject, state.request_digest);
      if (isActiveProject(operationProject)) {
        setCancelRequested(result.status === "cancel_requested");
        setMessage(result.status === "cancel_requested" ? "Cancellation requested before publication." : `Export is already ${result.status}.`);
        await refresh(false);
      }
    } catch (requestError) {
      if (isActiveProject(operationProject)) setError(readableError(requestError));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function saveVersion() {
    const operationProject = projectId;
    const operationVersion = versionId.trim();
    setBusy("save");
    setError("");
    try {
      await saveClientExport(operationProject, operationVersion);
      if (isActiveProject(operationProject)) {
        setMessage(`Saved immutable version ${operationVersion}.`);
        setVersionId("");
        await changed(operationProject);
      }
    } catch (requestError) {
      if (isActiveProject(operationProject)) setError(readableError(requestError));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function removeVersion(target: string) {
    const operationProject = projectId;
    setBusy("delete");
    setError("");
    try {
      await deleteClientExport(operationProject, target);
      if (isActiveProject(operationProject)) {
        setMessage(`Deleted saved version ${target}.`);
        setConfirmDelete(null);
        await changed(operationProject);
      }
    } catch (requestError) {
      if (isActiveProject(operationProject)) setError(readableError(requestError));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  async function recover() {
    const operationProject = projectId;
    setBusy("recover");
    setError("");
    try {
      const result = await recoverClientExports(operationProject);
      if (isActiveProject(operationProject)) {
        setMessage(`Recovery check: ${result.status.replaceAll("_", " ")}.`);
        await changed(operationProject);
      }
    } catch (requestError) {
      if (isActiveProject(operationProject)) setError(readableError(requestError));
    } finally {
      if (mounted.current) setBusy(null);
    }
  }

  return (
    <section className={`export-center ${compact ? "is-compact" : ""}`} aria-labelledby={`export-center-${compact ? "compact" : "full"}`}>
      <header className="export-center-heading">
        <div>
          <span>Explicit client output</span>
          <h2 id={`export-center-${compact ? "compact" : "full"}`}>Export center</h2>
          <p>Generate only on command. Finalize, review saves and page refreshes never create client files.</p>
        </div>
        <span className={`export-state is-${loading && !payload ? "loading" : state?.status ?? "absent"}`} role="status" aria-live="polite">
          {loading || generatingProjectId !== null || rendering || publishing ? <LoaderCircle className="spin" /> : currentIsUsable ? <CheckCircle2 /> : <Archive />}
          {loading && !payload ? "loading" : anotherProjectGenerating ? "another project exporting" : generating && !rendering && !publishing ? "preparing" : state?.status?.replaceAll("_", " ") ?? "absent"}
        </span>
      </header>

      {!allowed && (
        <div className="export-gate" role="note">
          <AlertTriangle />
          <div><strong>Generation is blocked</strong><p>{blockedReasons[0] ?? "Finalize a current, professionally ready evidence package first."}</p></div>
        </div>
      )}

      <div className="export-control-board">
        <fieldset className="export-format-switch" disabled={actionLocked}>
          <legend>Output package</legend>
          {(["xlsx", "pdf", "both"] as ExportSelection[]).map((value) => (
            <label key={value} className={selection === value ? "is-selected" : ""}>
              <input type="radio" name={`export-format-${compact ? "compact" : "full"}`} value={value} checked={selection === value} onChange={() => setSelection(value)} />
              {value === "xlsx" ? <FileSpreadsheet /> : value === "pdf" ? <FileText /> : <Archive />}
              <span><strong>{value === "both" ? "Both" : value.toUpperCase()}</strong><small>{value === "xlsx" ? "Editable workbook" : value === "pdf" ? "Client-ready document" : "One synchronized package"}</small></span>
            </label>
          ))}
        </fieldset>

        {!compact && (
          <div className="export-settings-grid">
            <label>Language<select value={language} onChange={(event) => setLanguage(event.target.value)} disabled={actionLocked}><option value="bilingual">Bilingual</option><option value="zh">Chinese</option><option value="en">English</option></select></label>
            <label>Density<select value={density} onChange={(event) => setDensity(event.target.value)} disabled={actionLocked}><option value="client">Client · 4 shots</option><option value="compact">Compact · 8 shots</option></select></label>
            <label className="is-wide">Project subtitle<input value={subtitle} onChange={(event) => setSubtitle(event.target.value)} placeholder="Optional client-facing subtitle" disabled={actionLocked} /></label>
            <label>Logo path<input value={logoPath} onChange={(event) => setLogoPath(event.target.value)} placeholder="assets/client-logo.png" disabled={actionLocked} /></label>
            <label>Accent<input type="color" value={accentColor} onChange={(event) => setAccentColor(event.target.value.toUpperCase())} disabled={actionLocked} /></label>
          </div>
        )}

        <div className="export-primary-actions">
          <button type="button" className="primary-button" onClick={() => void generate()} disabled={!allowed || actionLocked}>
            {generating || rendering ? <LoaderCircle className="spin" /> : <Archive />}
            {rendering ? "Rendering…" : publishing ? "Publishing…" : `Generate ${selection === "both" ? "both" : selection.toUpperCase()}`}
          </button>
          {rendering && <button type="button" className="secondary-button" onClick={() => void cancel()} disabled={busy !== null || cancelRequested}><X />{cancelRequested ? "Cancellation requested…" : "Cancel before publish"}</button>}
          {state?.status === "failed" && <button type="button" className="secondary-button" onClick={() => void recover()} disabled={busy !== null}><RefreshCw />Check recovery</button>}
        </div>
      </div>

      {(error || message) && <p className={`export-feedback ${error ? "is-error" : "is-success"}`} role={error ? "alert" : "status"} aria-live="polite">{error ? <AlertTriangle /> : <CheckCircle2 />}{error || message}</p>}

      <div className="export-current-strip">
        <div><span>Current</span><strong>{current ? current.receipt.formats.map((item) => item.toUpperCase()).join(" + ") : "Not generated"}</strong></div>
        <div><span>Lifecycle</span><strong className={current?.lifecycle_state === "stale" ? "is-stale" : ""}>{current?.lifecycle_state ?? "absent"}</strong></div>
        <div><span>Generated</span><strong>{current ? new Date(current.receipt.created_at_utc).toLocaleString() : "—"}</strong></div>
        <div><span>Package ID</span><code>{current?.receipt.export_id.slice(0, 12) ?? "—"}</code></div>
      </div>
      {current && (
        <div className="export-downloads" aria-label="Current package downloads">
          <span>{allowed && currentIsUsable ? "Verified current files" : "Historical package · not current"}</span>
          {(!allowed || !currentIsUsable) && <strong className="download-blocked"><AlertTriangle />Stale—inspect only; do not present as current</strong>}
          {current.receipt.formats.map((format) => current.downloads[format] ? (
            <a key={format} className="secondary-button" href={assetUrl(current.downloads[format])} target="_blank" rel="noreferrer"><Download />Download {allowed && currentIsUsable ? "" : "historical "}{format.toUpperCase()}</a>
          ) : null)}
        </div>
      )}

      {!compact && (
        <div className="export-version-layout">
          <div className="save-version-form">
            <div><span>Immutable snapshot</span><h3>Save current as a version</h3><p>Off by default. Saving copies only the verified current package.</p></div>
            <label>Version ID<input value={versionId} onChange={(event) => setVersionId(event.target.value)} placeholder="client-review-v1" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,127}" disabled={!currentIsUsable || actionLocked} /></label>
            <button type="button" className="secondary-button" onClick={() => void saveVersion()} disabled={!currentIsUsable || !versionId.trim() || actionLocked}><Save />Save version</button>
          </div>

          <div className="saved-version-list" aria-label={`${payload?.saved.length ?? 0} saved export versions`}>
            <div className="saved-version-heading"><h3>Saved versions</h3><span>{payload?.saved.length ?? 0}</span></div>
            {payload?.saved.length ? payload.saved.map((item) => (
              <div className="saved-version-row" key={item.version_id}>
                <div><strong>{item.version_id}</strong><small>{item.formats.map((format) => format.toUpperCase()).join(" + ")} · {formatBytes(item.size_bytes)}</small></div>
                <div className="saved-downloads">{item.formats.map((format) => item.downloads[format] ? <a key={format} href={assetUrl(item.downloads[format])} target="_blank" rel="noreferrer" aria-label={`Download saved ${format.toUpperCase()} from ${item.version_id}`}><Download />{format.toUpperCase()}</a> : null)}</div>
                {confirmDelete === item.version_id ? (
                  <div className="delete-confirm"><button type="button" className="danger-button" onClick={() => void removeVersion(item.version_id)} disabled={busy !== null}><Trash2 />Confirm</button><button type="button" className="icon-button" onClick={() => setConfirmDelete(null)} aria-label={`Cancel deletion of ${item.version_id}`}><X /></button></div>
                ) : <button type="button" className="icon-button" onClick={() => setConfirmDelete(item.version_id)} aria-label={`Delete saved version ${item.version_id}`} disabled={busy !== null}><Trash2 /></button>}
              </div>
            )) : <p className="export-empty">No saved versions. Generating current does not create history.</p>}
          </div>
        </div>
      )}
    </section>
  );
}

function formatBytes(value?: number): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}
