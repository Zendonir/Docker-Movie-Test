import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, FileText, RefreshCw, Wrench } from "lucide-react";
import { api } from "../api";
import { fmtBytes, fmtDate, fmtDuration, fmtInt, fmtPosition, METHOD_LABELS, PRECISION_LABELS } from "../format";
import type { DetectedError, FileDetail, Method, ScanResult } from "../types";
import { Card, Empty, ErrorBox, Loading, SeverityBadge, StatusBadge } from "../components/ui";
import { Timeline } from "../components/Timeline";
import { RecheckModal } from "./Files";
import { RepairDialog } from "../components/RepairDialog";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-sm [overflow-wrap:anywhere]">{children ?? "–"}</dd>
    </div>
  );
}

function ResultCard({ method, r }: { method: Method; r?: ScanResult }) {
  return (
    <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{METHOD_LABELS[method]}</span>
        {r ? <StatusBadge status={r.status} small /> : <span className="text-xs text-slate-500">nicht durchgeführt</span>}
      </div>
      {r && (
        <div className="mt-2 space-y-0.5 text-xs text-slate-500">
          <div>{fmtDate(r.finished_at)} · {r.outcome_label} · {Math.round(r.duration_s)} s</div>
          {r.summary && <div>{r.summary}</div>}
          {method === "full" && r.decoded_duration != null && (
            <div>Decodiert bis {fmtDuration(r.decoded_duration)} von {fmtDuration(r.container_duration)}</div>
          )}
          <div className="truncate" title={Object.keys(r.tools).join(", ")}>Werkzeuge: {Object.keys(r.tools).join(", ") || "–"}</div>
        </div>
      )}
    </div>
  );
}

function ErrorDetail({ e, fileId }: { e: DetectedError; fileId: number }) {
  const [log, setLog] = useState<string | null>(null);
  const loadLog = async () => setLog(await api<string>(`/api/files/${fileId}/results/${e.result_id}/log`));
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={e.severity} heuristic={e.heuristic} />
        <span className="font-medium">{e.error_type_label}</span>
      </div>
      <p>{e.description}</p>
      {e.heuristic && <p className="text-xs text-slate-500">Hinweis: Dieser Befund beruht auf einer Heuristik und ist kein zweifelsfreier Defektnachweis.</p>}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <Field label="Zeitpunkt">{fmtPosition(e.time_start, e.time_end, e.time_precision)}</Field>
        <Field label="Genauigkeit">{PRECISION_LABELS[e.time_precision]}</Field>
        <Field label="Zeitquelle">{e.time_source ?? "–"}</Field>
        <Field label="Stream">{e.stream_index != null ? `#${e.stream_index} ${e.stream_type ?? ""}` : "–"}</Field>
        <Field label="Byte-Offset">{e.byte_offset != null ? `${fmtInt(e.byte_offset)}${e.byte_end != null ? ` – ${fmtInt(e.byte_end)}` : ""}` : "–"}</Field>
        <Field label="Werkzeug / Methode">{e.tool} · {e.method_label}</Field>
        <Field label="Anzahl Meldungen">{fmtInt(e.occurrences)}</Field>
        {Array.isArray(e.extra?.confirmed_by) && <Field label="Bestätigt durch">{(e.extra.confirmed_by as string[]).join(", ")}</Field>}
      </dl>
      <div>
        <div className="label">Original-Logmeldung</div>
        <pre className="max-h-64 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200 whitespace-pre-wrap">{e.raw_message || "(keine Meldung gespeichert)"}</pre>
      </div>
      <button className="btn-secondary" onClick={loadLog}><FileText className="h-4 w-4" />Vollständiges Prüfprotokoll laden</button>
      {log !== null && <pre className="max-h-96 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] text-slate-200 whitespace-pre-wrap">{log}</pre>}
    </div>
  );
}

export function FileDetailPage() {
  const { id } = useParams();
  const fileId = Number(id);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["file", fileId], queryFn: () => api<FileDetail>(`/api/files/${fileId}`) });
  const [selected, setSelected] = useState<number | null>(null);
  const [recheck, setRecheck] = useState(false);
  const [repair, setRepair] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const f = q.data;
  const errors = useMemo(() => (f?.errors ?? []).filter((e) => showInfo || e.severity !== "info"), [f, showInfo]);
  if (q.isLoading) return <Loading />;
  if (q.error || !f) return <ErrorBox error={q.error ?? "Nicht gefunden"} />;
  const sel = errors.find((e) => e.id === selected) ?? null;
  const exp = (format: string) => `/api/export?format=${format}&scope=file&file_id=${f.id}`;

  return (
    <div className="space-y-4">
      <Link to="/dateien" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-sky-500"><ArrowLeft className="h-4 w-4" />Zur Dateiliste</Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold [overflow-wrap:anywhere]">{f.filename}</h1>
          <p className="text-sm text-slate-500 [overflow-wrap:anywhere]">{f.path}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={f.status} />
            <span className="text-xs text-slate-500">Prüftiefe: {f.check_level_label}</span>
            {!f.present && <span className="rounded bg-red-500/15 px-2 py-0.5 text-xs text-red-500">Nicht mehr vorhanden seit {fmtDate(f.missing_since)}</span>}
          </div>
          {f.status_note && <p className="mt-1 text-xs text-slate-500">{f.status_note}</p>}
          <AssessmentBox a={f.assessment} />
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary" onClick={() => setRecheck(true)} disabled={!f.present}><RefreshCw className="h-4 w-4" />Erneut prüfen</button>
          <a className="btn-secondary" href={exp("html")} target="_blank" rel="noreferrer"><Download className="h-4 w-4" />HTML</a>
          <a className="btn-secondary" href={exp("csv")}><Download className="h-4 w-4" />CSV</a>
          <a className="btn-secondary" href={exp("json")}><Download className="h-4 w-4" />JSON</a>
          {f.present && f.extension === "mkv" && ["repair", "replace", "optional"].includes(f.assessment?.category) &&
            <button className="btn-secondary" onClick={() => setRepair(true)}><Wrench className="h-4 w-4" />Reparieren …</button>}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Datei" className="xl:col-span-2">
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Field label="Container">{f.container_format}</Field>
            <Field label="Laufzeit">{fmtDuration(f.duration)}</Field>
            <Field label="Dateigröße">{fmtBytes(f.size)} ({fmtInt(f.size)} Byte)</Field>
            <Field label="Bitrate">{f.bit_rate ? `${fmtInt(Math.round(f.bit_rate / 1000))} kbit/s` : "–"}</Field>
            <Field label="Video">{f.video_codec} {f.video_resolution}</Field>
            <Field label="Audio">{f.audio_codecs}</Field>
            <Field label="Letzte Prüfung">{fmtDate(f.last_checked_at)}</Field>
            <Field label="Verwendete Prüfmethoden">{f.methods_done.length ? f.methods_done.map((m) => METHOD_LABELS[m]).join(", ") : "–"}</Field>
            <Field label="Erstmals erfasst">{fmtDate(f.first_seen_at)}</Field>
            <Field label="Bibliothek">{f.library}</Field>
            {f.moved_from && <Field label="Verschoben von">{f.moved_from}</Field>}
            {f.quick_hash && <Field label="Schnell-Hash">{f.quick_hash.slice(0, 16)}…</Field>}
          </dl>
        </Card>
        <Card title="Prüfergebnisse (aktuelle Dateiversion)">
          <div className="space-y-2">
            {(["quick", "container", "full"] as Method[]).map((m) => <ResultCard key={m} method={m} r={f.current_results[m]} />)}
          </div>
        </Card>
      </div>

      <Card title="Streams">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px]">
            <thead><tr><th className="th">#</th><th className="th">Typ</th><th className="th">Codec</th><th className="th">Details</th><th className="th">Sprache</th><th className="th">Bitrate</th><th className="th">Decodierbar</th></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {f.streams.map((s) => (
                <tr key={s.index}>
                  <td className="td tabular-nums">{s.index}</td>
                  <td className="td">{{ video: "Video", audio: "Audio", subtitle: "Untertitel", attachment: "Anhang", data: "Daten" }[s.type] ?? s.type}{s.is_default && <span className="ml-1 text-[10px] text-slate-500">(Standard)</span>}</td>
                  <td className="td"><span className="font-medium">{s.codec_name ?? "unbekannt"}</span>{s.profile && <span className="text-xs text-slate-500"> · {s.profile}</span>}<div className="text-[11px] text-slate-500">{s.codec_long_name}</div></td>
                  <td className="td text-xs">
                    {s.type === "video" && <>{s.width}×{s.height}{s.fps ? ` · ${s.fps.toLocaleString("de-DE", { maximumFractionDigits: 3 })} fps` : ""}{s.pix_fmt ? ` · ${s.pix_fmt}` : ""}{s.bit_depth ? ` · ${s.bit_depth} bit` : ""}{s.attached_pic ? " · Coverbild" : ""}</>}
                    {s.type === "audio" && <>{s.channels} Kanäle{s.channel_layout ? ` (${s.channel_layout})` : ""} · {s.sample_rate ? `${fmtInt(s.sample_rate)} Hz` : ""}</>}
                    {s.title && <div className="text-slate-500">{s.title}</div>}
                  </td>
                  <td className="td">{s.language ?? "–"}</td>
                  <td className="td text-xs tabular-nums">{s.bit_rate ? `${fmtInt(Math.round(s.bit_rate / 1000))} kbit/s` : "–"}</td>
                  <td className="td text-xs">{s.type === "video" || s.type === "audio" ? (s.decodable ? "ja" : <span className="text-violet-400">nein (nicht unterstützt)</span>) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {f.streams.length === 0 && <Empty>Keine Streaminformationen (Datei noch nicht analysiert oder nicht lesbar).</Empty>}
        </div>
      </Card>

      <Card title="Zeitleiste der Fehlerstellen">
        <Timeline duration={f.duration} errors={errors} selected={selected} onSelect={setSelected} />
      </Card>

      <div className="grid gap-4 xl:grid-cols-5">
        <Card title={`Befunde (${errors.length})`} className="xl:col-span-3"
          actions={<label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={showInfo} onChange={(e) => setShowInfo(e.target.checked)} />Infos anzeigen</label>}>
          {errors.length === 0 ? <Empty>{f.check_level === "none" ? "Noch nicht geprüft." : "Keine Befunde – die durchgeführten Prüfungen haben keine Fehler erkannt."}</Empty> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px]">
                <thead><tr><th className="th">Zeitpunkt</th><th className="th">Schweregrad</th><th className="th">Fehler</th><th className="th">Stream</th><th className="th">Werkzeug</th></tr></thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {errors.map((e) => (
                    <tr key={e.id} onClick={() => setSelected(e.id)} className={`cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/40 ${selected === e.id ? "bg-sky-500/10" : ""}`}>
                      <td className="td whitespace-nowrap text-xs tabular-nums">
                        {fmtPosition(e.time_start, e.time_end, e.time_precision)}
                        {e.time_precision === "unknown" && <div className="text-[10px] text-slate-500">keine genaue Zeitposition</div>}
                      </td>
                      <td className="td"><SeverityBadge severity={e.severity} heuristic={e.heuristic} /></td>
                      <td className="td"><div className="font-medium">{e.error_type_label}</div><div className="line-clamp-2 text-xs text-slate-500">{e.description}</div></td>
                      <td className="td text-xs">{e.stream_index != null ? `#${e.stream_index}` : "–"}</td>
                      <td className="td text-xs text-slate-500">{e.tool}{e.occurrences > 1 && <div>{fmtInt(e.occurrences)}×</div>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <Card title="Details und Logdaten" className="xl:col-span-2">
          {sel ? <ErrorDetail e={sel} fileId={f.id} /> : <Empty>Befund in der Liste oder auf der Zeitleiste anklicken.</Empty>}
        </Card>
      </div>

      <Card title="Prüfverlauf">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px]">
            <thead><tr><th className="th">Zeitpunkt</th><th className="th">Methode</th><th className="th">Ergebnis</th><th className="th">Ablauf</th><th className="th">Befunde</th><th className="th">Version</th><th className="th">Auftrag</th></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {f.history.map((r) => (
                <tr key={r.id} className={r.is_current ? "" : "opacity-60"}>
                  <td className="td text-xs">{fmtDate(r.finished_at)}</td>
                  <td className="td text-sm">{r.method_label}</td>
                  <td className="td"><StatusBadge status={r.status} small /></td>
                  <td className="td text-xs">{r.outcome_label}{r.is_current ? "" : " · veraltet"}</td>
                  <td className="td text-xs tabular-nums">{r.critical_count + r.error_count} Fehler · {r.warning_count} Warn.</td>
                  <td className="td text-xs">Prüfer v{r.scanner_version} · Datei v{r.fingerprint_version}</td>
                  <td className="td text-xs">{r.job_id ? <Link className="hover:text-sky-500" to={`/scans/${r.job_id}`}>#{r.job_id}</Link> : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {f.history.length === 0 && <Empty>Noch keine Prüfungen.</Empty>}
        </div>
      </Card>
      {recheck && <RecheckModal open onClose={() => setRecheck(false)} fileIds={[f.id]} onDone={() => qc.invalidateQueries({ queryKey: ["live"] })} />}
      {repair && <RepairDialog title={`Reparieren: ${f.filename}`} fileIds={[f.id]}
        salvageDefault={f.assessment.category === "replace"} showSalvage={f.assessment.category === "replace"}
        onClose={() => setRepair(false)} />}
    </div>
  );
}

const ASSESS_STYLE: Record<string, string> = {
  replace: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400",
  repair: "border-orange-500/40 bg-orange-500/10 text-orange-600 dark:text-orange-400",
  optional: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  unchecked: "border-slate-500/40 bg-slate-500/10 text-slate-600 dark:text-slate-400",
};

function AssessmentBox({ a }: { a?: FileDetail["assessment"] }) {
  if (!a) return null;
  return (
    <div className={`mt-2 max-w-3xl rounded border px-3 py-2 text-sm ${ASSESS_STYLE[a.category] ?? ""}`}>
      <div className="font-semibold">Empfehlung: {a.label}</div>
      {a.reasons.length > 0 && <div className="text-xs">{a.reason_text}</div>}
      {a.repaired ? (
        <div className="mt-1 text-xs font-semibold text-green-600 dark:text-green-400">
          Repariert{a.repaired.repaired_at ? ` am ${fmtDate(a.repaired.repaired_at)}` : ""} → {a.repaired.output_path}
          <span className="block font-normal opacity-80">Original noch unverändert – nach dem Ersetzen wird die Datei neu geprüft.</span>
        </div>
      ) : <div className="text-xs opacity-80">{a.action}</div>}
    </div>
  );
}
