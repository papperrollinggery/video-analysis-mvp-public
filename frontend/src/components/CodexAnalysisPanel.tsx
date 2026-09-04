import { ReactNode, useEffect, useRef, useState } from "react";
import { Clipboard, RefreshCw, ShieldCheck } from "lucide-react";
import {
  applyCodexAnalysis,
  CodexAnalysisRequest,
  CodexAnalysisStatus,
  getCodexAnalysisStatus,
  prepareCodexAnalysis,
  readableError
} from "../api/client";

const maxResponseBytes = 1024 * 1024;

export function CodexAnalysisPanel({
  projectId, onApplied, children
}: {
  projectId: string;
  onApplied: () => Promise<void>;
  children?: ReactNode;
}) {
  const [status, setStatus] = useState<CodexAnalysisStatus | null>(null);
  const [prepared, setPrepared] = useState<CodexAnalysisRequest | null>(null);
  const [responseText, setResponseText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const statusEpoch = useRef(0);

  useEffect(() => {
    const epoch = ++statusEpoch.current;
    void getCodexAnalysisStatus(projectId).then(
      (value) => { if (epoch === statusEpoch.current) setStatus(value); },
      (failure) => { if (epoch === statusEpoch.current) setError(readableError(failure)); }
    );
    return () => { statusEpoch.current += 1; };
  }, [projectId]);

  const brief = `Use Video Evidence Workbench for project ${JSON.stringify(projectId)} in its existing workflow. Read data/codex_analysis_request.json in this project's directory and follow its versioned guide. Inspect the exact listed image and audio/text evidence with your available tools. Produce the required codex-analysis-response/v1 JSON and submit it through the tool's codex apply command. Do not edit shots.json, fabricate human review, run another research workflow, Finalize, or generate Excel/PDF without the corresponding explicit request.`;

  async function prepare() {
    const epoch = ++statusEpoch.current;
    setBusy(true); setError(""); setMessage("");
    try {
      const value = await prepareCodexAnalysis(projectId);
      if (epoch !== statusEpoch.current) return;
      setPrepared(value);
      setStatus({ status: "prepared", request_id: value.request.request_id, selected_shot_count: value.request.shots.length, review_required: true, api_key_required: false });
      const count = value.request.shots.length;
      setMessage(`Prepared ${count} ${count === 1 ? "shot" : "shots"}. Open the current project in Codex and use the tool-running brief below.`);
    } catch (failure) { if (epoch === statusEpoch.current) setError(readableError(failure)); }
    finally { if (epoch === statusEpoch.current) setBusy(false); }
  }

  async function apply() {
    const epoch = ++statusEpoch.current;
    setBusy(true); setError(""); setMessage("");
    try {
      if (new TextEncoder().encode(responseText).length > maxResponseBytes) throw new Error("Response exceeds 1 MiB.");
      JSON.parse(responseText);
      const result = await applyCodexAnalysis(projectId, responseText);
      if (epoch !== statusEpoch.current) return;
      setMessage(`${result.result.summary} Human review is still required. Finalize remains a separate action.`);
      if (result.status === "applied") setResponseText("");
      try {
        await onApplied();
        const currentStatus = await getCodexAnalysisStatus(projectId);
        if (epoch === statusEpoch.current) setStatus(currentStatus);
      } catch (failure) {
        if (epoch === statusEpoch.current) setError(`Analysis submission was received, but refreshing the workspace failed: ${readableError(failure)}. Reload before submitting again.`);
      }
    } catch (failure) { if (epoch === statusEpoch.current) setError(readableError(failure)); }
    finally { if (epoch === statusEpoch.current) setBusy(false); }
  }

  async function copyBrief() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await Promise.race([
        navigator.clipboard.writeText(brief),
        new Promise<never>((_, reject) => window.setTimeout(() => reject(new Error("Clipboard timed out")), 1200))
      ]);
      setMessage("Tool-running brief copied. No media was uploaded.");
    } catch { setError("Clipboard unavailable. Select and copy the brief manually."); }
  }

  async function loadResponse(file?: File) {
    if (!file) return;
    try {
      if (file.size > maxResponseBytes) throw new Error("Response exceeds 1 MiB.");
      setResponseText(await file.text());
      setError("");
    } catch (failure) { setError(readableError(failure)); }
  }

  return (
    <div className="codex-panel">
      <div className="boundary-note"><ShieldCheck /><div><strong>Current Codex · same tool workflow</strong><p>No additional API key. This panel prepares evidence; it does not launch a model or upload files. Codex results return as model proposals, not human approval.</p></div></div>
      <p className="small-note">State: <strong>{status?.status ?? "checking"}</strong>{status?.selected_shot_count !== undefined ? ` · ${status.selected_shot_count} ${status.selected_shot_count === 1 ? "shot" : "shots"}` : ""}</p>
      {status?.reason && <p className="small-note">{status.reason}</p>}
      <ol>
        <li>Prepare one current, version-bound analysis request.</li>
        <li>Let the current Codex task inspect the listed evidence and follow the built-in guide.</li>
        <li>Apply its structured response, then use the existing review and Finalize controls.</li>
      </ol>
      <button className="primary-button" onClick={() => void prepare()} disabled={busy}><RefreshCw />{busy ? "Working…" : "Prepare Codex analysis"}</button>
      {prepared && <p className="small-note">Current request: <code>{prepared.request_path}</code>. Re-preparing replaces this slot; it does not create document versions.</p>}
      <label htmlFor="codex-running-brief">Tool-running brief</label>
      <textarea id="codex-running-brief" value={brief} readOnly rows={7} />
      <button className="secondary-button" onClick={() => void copyBrief()}><Clipboard />Copy brief</button>
      {prepared && <details><summary>Built-in guide and response schema</summary><ol>{prepared.request.guide.map((line) => <li key={line}>{line}</li>)}</ol><textarea aria-label="Required response schema" readOnly rows={9} value={JSON.stringify(prepared.request.response_schema, null, 2)} /></details>}
      <label htmlFor="codex-response-file">Load a response JSON file (kept local until Apply)</label>
      <input id="codex-response-file" type="file" accept="application/json,.json" disabled={busy} onChange={(event) => void loadResponse(event.target.files?.[0])} />
      <label htmlFor="codex-response">Structured response</label>
      <textarea id="codex-response" rows={8} value={responseText} disabled={busy} onChange={(event) => setResponseText(event.target.value)} placeholder="Paste codex-analysis-response/v1 JSON" />
      <button className="primary-button" onClick={() => void apply()} disabled={busy || !responseText.trim()}>Apply model analysis</button>
      <p className="small-note">Apply checks project, shot and frame versions. It does not mark human review complete, Finalize, or generate Excel/PDF.</p>
      <p role="status" aria-live="polite" className="small-note">{message}</p>
      {error && <p role="alert" className="copy-status">{error}</p>}
      {children}
    </div>
  );
}
