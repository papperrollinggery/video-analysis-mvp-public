import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Gauge,
  Headphones,
  Mic2,
  Music2,
  RefreshCw,
  Save,
  ShieldCheck,
  Volume2,
  Waves
} from "lucide-react";
import { apiErrorCode, getAudioReview, readableError, saveAudioReview } from "../../api/client";
import type {
  AudioEvent,
  AudioEventKind,
  AudioProposal,
  AudioReviewPage,
  AudioReviewRequest,
  AudioReviewResult,
  AudioReviewStatus
} from "../../types";

const kinds: Array<{ value: AudioEventKind; label: string; icon: typeof Mic2 }> = [
  { value: "voice", label: "VO / dialogue", icon: Mic2 },
  { value: "music", label: "Music", icon: Music2 },
  { value: "sfx", label: "Sound effects", icon: Volume2 },
  { value: "mixed", label: "Unclassified / mixed", icon: Waves },
  { value: "silence", label: "Threshold silence", icon: Gauge }
];

const kindLabel = Object.fromEntries(kinds.map((item) => [item.value, item.label])) as Record<AudioEventKind, string>;

type ReviewFilter = "all" | "unreviewed" | AudioReviewStatus | "needs_review";

export function AudioReviewPanel({
  projectId,
  durationSeconds,
  selectedShotId,
  onSeek,
  onCue,
  onReviewSaved
}: {
  projectId: string;
  durationSeconds: number;
  selectedShotId?: string;
  onSeek: (seconds: number) => void;
  onCue: (seconds: number) => void;
  onReviewSaved: (result: AudioReviewResult) => Promise<void>;
}) {
  const [page, setPage] = useState<AudioReviewPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<"all" | AudioEventKind>("all");
  const [reviewFilter, setReviewFilter] = useState<ReviewFilter>("needs_review");
  const [onlyShot, setOnlyShot] = useState(false);
  const shotFilter = onlyShot ? selectedShotId : undefined;
  const cursorScope = shotFilter ?? "all-shots";
  const [cursor, setCursor] = useState({ scope: cursorScope, offset: 0 });
  const offset = cursor.scope === cursorScope ? cursor.offset : 0;
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [selectedEventId, setSelectedEventId] = useState("");
  const requestEpoch = useRef(0);
  const generationId = useRef<string | undefined>(undefined);

  useEffect(() => {
    const epoch = ++requestEpoch.current;
    setLoading(true);
    setError("");
    setPage(null);
    void getAudioReview(projectId, {
      offset,
      limit: 50,
      kind: kind === "all" ? undefined : kind,
      review_status: reviewFilter === "all" ? undefined : reviewFilter,
      shot_id: shotFilter,
      expected_generation_id: offset > 0 ? generationId.current : undefined
    }).then((next) => {
      if (epoch !== requestEpoch.current) return;
      setPage(next);
      generationId.current = next.generation_id ?? undefined;
      setSelectedEventId((current) => next.events.some((event) => event.event_id === current)
        ? current
        : next.events[0]?.event_id ?? "");
    }).catch((requestError) => {
      if (epoch !== requestEpoch.current) return;
      if (apiErrorCode(requestError) === "stale_generation") {
        generationId.current = undefined;
        setError("Audio evidence changed. Refresh from the first page before continuing.");
      } else {
        setError(readableError(requestError));
      }
    }).finally(() => {
      if (epoch === requestEpoch.current) setLoading(false);
    });
  }, [projectId, offset, kind, reviewFilter, shotFilter, refreshNonce]);

  function changeFilter(nextKind: "all" | AudioEventKind, nextReview = reviewFilter) {
    setKind(nextKind);
    setReviewFilter(nextReview);
    setCursor({ scope: cursorScope, offset: 0 });
    generationId.current = undefined;
  }

  function selectEvent(event: AudioEvent) {
    setSelectedEventId(event.event_id);
    onSeek(event.start_time);
  }

  const selected = page?.events.find((event) => event.event_id === selectedEventId);
  const timelineAvailable = durationSeconds > 0;
  const totalDuration = Math.max(durationSeconds, 0.001);
  const rows = useMemo(() => kinds.map((lane) => ({
    ...lane,
    events: page?.events.filter((event) => event.kind === lane.value) ?? []
  })), [page?.events]);

  return (
    <section className="audio-review-section" aria-labelledby="audio-review-title">
      <header className="audio-review-header">
        <div>
          <span className="audio-section-index">AUDIO / EVIDENCE</span>
          <h2 id="audio-review-title">Sound timeline & operator review</h2>
          <p>Original proposals stay immutable. Reviews change the effective view, then require an explicit Finalize.</p>
        </div>
        <div className="audio-review-summary" aria-label="Audio review summary">
          <span><strong>{page?.page.total ?? 0}</strong> matches</span>
          <span title={page?.counts_scope}><strong>{page?.requires_review_count ?? "—"}</strong> need review · all</span>
          <span title={page?.counts_scope}><strong>{page?.review_counts.reviewed ?? 0}</strong> reviewed · all</span>
        </div>
      </header>

      <div className="audio-filter-bar" aria-label="Audio filters">
        <label>Layer<select value={kind} onChange={(event) => changeFilter(event.target.value as "all" | AudioEventKind)}>
          <option value="all">All audio layers</option>
          {kinds.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select></label>
        <label>Review state<select value={reviewFilter} onChange={(event) => {
          setReviewFilter(event.target.value as ReviewFilter);
          setCursor({ scope: cursorScope, offset: 0 });
          generationId.current = undefined;
        }}>
          <option value="needs_review">Needs review</option>
          <option value="all">All states</option>
          <option value="unreviewed">Unreviewed</option>
          <option value="reviewed">Reviewed</option>
          <option value="needs_work">Needs work</option>
          <option value="rejected">Rejected</option>
        </select></label>
        <label className="audio-shot-filter">
          <input type="checkbox" checked={onlyShot} disabled={!selectedShotId} onChange={(event) => {
            setOnlyShot(event.target.checked);
            setCursor({ scope: event.target.checked && selectedShotId ? selectedShotId : "all-shots", offset: 0 });
            generationId.current = undefined;
          }} />
          <span>Only selected shot<small>{selectedShotId ?? "Select a shot first"}</small></span>
        </label>
        <button className="secondary-button audio-refresh" type="button" onClick={() => {
          setCursor({ scope: cursorScope, offset: 0 });
          generationId.current = undefined;
          requestEpoch.current += 1;
          setPage(null);
          setRefreshNonce((value) => value + 1);
        }}><RefreshCw /> Refresh</button>
      </div>

      {error && <div className="audio-inline-error" role="alert"><AlertTriangle /><span>{error}</span></div>}
      {loading && <div className="audio-loading" role="status"><CircleDashed className="spin" /><span>Verifying the current audio binding…</span></div>}

      {!loading && page && !page.available && (
        <div className="audio-empty-state"><Headphones /><div><h3>No audio timeline yet</h3><p>{page.reason}</p><code>analyze-video audio {projectId} --skip-asr</code></div></div>
      )}

      {!loading && page?.available && (
        <>
          <div className="audio-capability-strip" aria-label="Audio capability status">
            {Object.entries(page.capabilities).map(([name, capability]) => (
              <span key={name} className={`is-${capability.status}`} title={capability.reason ?? undefined}>
                {capability.status === "produced" ? <CheckCircle2 /> : <AlertTriangle />}
                <strong>{humanize(name)}</strong> {humanize(capability.status)}
              </span>
            ))}
          </div>
          <p className="audio-data-trust"><ShieldCheck />{page.data_trust}</p>

          <div className="audio-review-layout">
            <div className="audio-timeline-column">
              {timelineAvailable ? <><div className="audio-time-ruler" aria-hidden="true">
                <span>00:00</span><span>{formatAudioTime(totalDuration / 2)}</span><span>{formatAudioTime(totalDuration)}</span>
              </div>
              <div className="audio-lanes" aria-label="Audio timeline layers">
                {rows.map(({ value, label, icon: Icon, events }) => (
                  <div className={`audio-lane is-${value}`} key={value}>
                    <span className="audio-lane-label"><Icon />{label}</span>
                    <div className="audio-lane-track">
                      {events.map((event) => {
                        const left = Math.min(100, Math.max(0, event.start_time / totalDuration * 100));
                        const width = Math.max(0, Math.min(100 - left, (event.end_time - event.start_time) / totalDuration * 100));
                        const style = { "--event-left": `${left}%`, "--event-width": `${width}%` } as CSSProperties;
                        const state = `${event.event_id === selectedEventId ? "is-selected" : ""} ${event.requires_review ? "needs-review" : ""}`;
                        return <span className="audio-event-object" key={event.event_id}>
                          <span className={`audio-event-band ${state}`} style={style} aria-hidden="true" />
                          <button
                            style={style}
                            aria-pressed={event.event_id === selectedEventId}
                            aria-label={`${label}, ${formatRange(event)}, ${event.requires_review ? "needs review" : "review resolved"}`}
                            title={`${formatRange(event)} · ${eventDisplay(event)}`}
                            onClick={() => selectEvent(event)}
                          />
                        </span>;
                      })}
                    </div>
                  </div>
                ))}
              </div></> : <div className="audio-scale-unavailable"><AlertTriangle /><p>Timeline scale unavailable because media duration is unknown. Use the event list and source timecodes; empty rails are not shown as silence.</p></div>}

              {page.events.length ? (
                <ol className="audio-event-list" aria-label="Audio events on this page">
                  {page.events.map((event) => (
                    <li key={event.event_id}>
                      <button className={event.event_id === selectedEventId ? "is-selected" : ""} onClick={() => selectEvent(event)}>
                        <span className={`audio-kind-mark is-${event.kind}`} />
                        <span className="mono">{formatRange(event)}</span>
                        <strong>{eventDisplay(event)}</strong>
                        <small>{humanize(event.identity_status)} · {formatConfidence(event.proposal.confidence)}</small>
                        <span className={`audio-review-state is-${event.review?.status ?? "unreviewed"}`}>
                          {event.requires_review ? "Needs review" : humanize(event.review?.status ?? "resolved")}
                        </span>
                      </button>
                    </li>
                  ))}
                </ol>
              ) : <div className="audio-filter-empty"><Waves /><p>No events match these filters. This is not evidence of silence.</p></div>}

              <div className="audio-pagination" aria-label="Audio event pages">
                <button type="button" className="secondary-button" disabled={offset === 0} onClick={() => setCursor({ scope: cursorScope, offset: Math.max(0, offset - 50) })}><ChevronLeft /> Previous</button>
                <span>{page.page.total && offset < page.page.total ? `${offset + 1}–${Math.min(offset + page.page.limit, page.page.total)} of ${page.page.total}` : "0 events on this page"}</span>
                <button type="button" className="secondary-button" disabled={page.page.next_offset === null} onClick={() => setCursor({ scope: cursorScope, offset: page.page.next_offset ?? offset })}>
                  Next <ChevronRight />
                </button>
              </div>
            </div>

            <aside className="audio-event-inspector" aria-live="polite">
              {selected ? <AudioEventEditor
                key={`${selected.event_id}-${page.generation_id}`}
                event={selected}
                generationId={page.generation_id!}
                projectId={projectId}
                onCue={onCue}
                onSaved={async (result) => {
                  generationId.current = undefined;
                  setCursor({ scope: cursorScope, offset: 0 });
                  setRefreshNonce((value) => value + 1);
                  await onReviewSaved(result);
                }}
              /> : <div className="audio-no-selection"><Headphones /><p>Select an event to inspect its evidence.</p></div>}
            </aside>
          </div>
        </>
      )}
    </section>
  );
}

function AudioEventEditor({
  event,
  generationId,
  projectId,
  onCue,
  onSaved
}: {
  event: AudioEvent;
  generationId: string;
  projectId: string;
  onCue: (seconds: number) => void;
  onSaved: (result: AudioReviewResult) => Promise<void>;
}) {
  const effective = event.effective_proposal ?? {
    ...event.proposal,
    ...(event.review?.overrides ?? {})
  };
  const [status, setStatus] = useState<AudioReviewStatus>(event.review?.status ?? "needs_work");
  const [label, setLabel] = useState(effective.label);
  const [text, setText] = useState(effective.text);
  const [voiceRole, setVoiceRole] = useState(effective.voice_role);
  const [confidence, setConfidence] = useState(String(effective.confidence));
  const [notes, setNotes] = useState(event.review?.review_notes ?? "");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const voiceLike = event.kind === "voice" || event.kind === "mixed";

  async function save(reviewEvent: FormEvent) {
    reviewEvent.preventDefault();
    const numericConfidence = Number(confidence);
    if (!Number.isFinite(numericConfidence) || numericConfidence < 0 || numericConfidence > 1) {
      setError("Confidence must be a number from 0 to 1.");
      return;
    }
    if (!confirmed) {
      setError("Confirm that a real operator checked this event against the source audio.");
      return;
    }
    const inherited = event.review?.overrides ?? {};
    const overrides: AudioReviewRequest["overrides"] = status === "rejected" ? {} : {
      ...inherited,
      label,
      confidence: numericConfidence,
      ...(voiceLike ? { text, voice_role: voiceRole } : {})
    };
    setSaving(true);
    setError("");
    setMessage("Saving the bound review…");
    setConflict(false);
    try {
      const result = await saveAudioReview(projectId, event.event_id, {
        expected_generation_id: generationId,
        expected_proposal_sha256: event.proposal_sha256,
        status,
        overrides,
        review_notes: notes,
        confirm_operator_review: true
      });
      if (result.exports_generated !== false) throw new Error("Unexpected export side effect reported by the service.");
      setConfirmed(false);
      await onSaved(result);
    } catch (requestError) {
      const code = apiErrorCode(requestError);
      setConflict(code === "stale_generation" || code === "stale_proposal" || code === "audio_state_changed" || code === "audio_commit_failed");
      setError(readableError(requestError));
      setMessage("");
    } finally {
      setSaving(false);
    }
  }

  return <form className="audio-event-editor" onSubmit={save}>
    <div className="audio-event-heading">
      <div><span>{kindLabel[event.kind]}</span><h3>{eventDisplay(event)}</h3></div>
      <button type="button" className="secondary-button" onClick={() => onCue(event.start_time)}><Headphones /> Cue</button>
    </div>
    <p className="large-timecode">{formatRange(event)}</p>
    <dl className="audio-evidence-ledger">
      <div><dt>Source event</dt><dd>{event.event_id}</dd></div>
      <div><dt>Identity</dt><dd>{humanize(event.identity_status)}</dd></div>
      <div><dt>Proposal proof</dt><dd>{humanize(event.proposal.verification)}</dd></div>
      <div><dt>Energy / pulse</dt><dd>{formatMeasurement(event.proposal)}</dd></div>
    </dl>
    <details className="audio-original-proposal">
      <summary>Original immutable proposal</summary>
      <dl><div><dt>Label</dt><dd>{event.proposal.label || "Empty"}</dd></div><div><dt>Text</dt><dd>{event.proposal.text || "Empty"}</dd></div><div><dt>Confidence</dt><dd>{formatConfidence(event.proposal.confidence)}</dd></div></dl>
    </details>

    <label>Decision<select value={status} onChange={(change) => setStatus(change.target.value as AudioReviewStatus)} disabled={saving}>
      <option value="reviewed">Reviewed and accepted</option>
      <option value="needs_work">Needs more work</option>
      <option value="rejected">Reject this proposal</option>
    </select></label>
    {status !== "rejected" && <>
      <label>Effective label<input value={label} onChange={(change) => setLabel(change.target.value)} disabled={saving} /></label>
      {voiceLike && <label>Effective transcript<textarea rows={4} value={text} onChange={(change) => setText(change.target.value)} disabled={saving} /></label>}
      {voiceLike && <label>Voice role<select value={voiceRole} onChange={(change) => setVoiceRole(change.target.value as AudioProposal["voice_role"])} disabled={saving}>
        <option value="unknown">Unknown</option><option value="voice_over">Voice over</option><option value="dialogue">Dialogue</option><option value="singing">Singing</option>
      </select></label>}
      <label>Operator confidence<input type="number" min="0" max="1" step="0.01" value={confidence} onChange={(change) => setConfidence(change.target.value)} disabled={saving} /></label>
    </>}
    <label>Review notes<textarea rows={3} value={notes} onChange={(change) => setNotes(change.target.value)} disabled={saving} /></label>
    <label className="audio-confirmation"><input type="checkbox" checked={confirmed} onChange={(change) => setConfirmed(change.target.checked)} disabled={saving} /><span><strong>I checked this interval against the source audio.</strong><small>This assertion is not model authentication and is never set automatically.</small></span></label>
    {(error || message) && <div className={`audio-review-feedback ${error ? "is-error" : "is-success"}`} role={error ? "alert" : "status"}>{error ? <AlertTriangle /> : <CheckCircle2 />}<span>{error || message}</span></div>}
    <button className="primary-button audio-save-review" type="submit" disabled={saving || conflict}>{saving ? <CircleDashed className="spin" /> : <Save />}{saving ? "Saving…" : conflict ? "Refresh required" : "Save audio review"}</button>
  </form>;
}

function eventDisplay(event: AudioEvent): string {
  const proposal = event.effective_proposal ?? event.proposal;
  return proposal.text || proposal.label || `${kindLabel[event.kind]} event`;
}

function formatMeasurement(proposal: AudioProposal): string {
  const values = [
    proposal.energy === null ? null : `energy ${proposal.energy.toFixed(3)}`,
    proposal.onset_density === null ? null : `${proposal.onset_density.toFixed(2)} onsets/s`,
    proposal.estimated_bpm === null ? null : `${proposal.estimated_bpm.toFixed(1)} BPM`
  ].filter(Boolean);
  return values.join(" · ") || "No acoustic measurement";
}

function formatAudioTime(value: number): string {
  const minutes = Math.floor(value / 60);
  const seconds = value - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`;
}

function formatRange(event: Pick<AudioEvent, "start_time" | "end_time">): string {
  return `${formatAudioTime(event.start_time)} – ${formatAudioTime(event.end_time)}`;
}

function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function humanize(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
