import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, Pause, Play, Square } from "lucide-react";
import { api, patch, post, qs } from "../api";
import { fmtBytes, fmtDate, fmtEta, fmtInt, JOB_STATE_LABELS } from "../format";
import type { FileStatus, JobDetail, LiveState, Paged } from "../types";
import { Card, Empty, ErrorBox, Loading, MethodChips, Pagination, StatusBadge } from "../components/ui";
import { LiveJobCard } from "../components/LiveJobCard";
import { STATE_CLS } from "./Scans";
import { useToast } from "../components/toast";

interface Item { id: number; file_id: number; filename: string; directory: string; size: number; state: string; status: FileStatus | null; status_label: string | null; started_at: string | null; finished_at: string | null; attempts: number; message: string | null }

const ITEM_STATES: Record<string, string> = { pending: "Ausstehend", running: "Läuft", done: "Geprüft", skipped: "Übersprungen", failed: "Fehlgeschlagen", cancelled: "Abgebrochen" };

export function JobDetailPage() {
  const { id } = useParams();
  const jobId = Number(id);
  const qc = useQueryClient();
  const toast = useToast();
  const [state, setState] = useState("");
  const [page, setPage] = useState(1);
  const job = useQuery({ queryKey: ["job", jobId], queryFn: () => api<JobDetail>(`/api/jobs/${jobId}`), refetchInterval: 5000 });
  const items = useQuery({ queryKey: ["job-items", jobId, state, page], queryFn: () => api<Paged<Item>>(`/api/jobs/${jobId}/items${qs({ state: state || undefined, page, page_size: 50 })}`), refetchInterval: 5000 });
  const live = useQuery({ queryKey: ["live"], queryFn: () => api<LiveState>("/api/scan/live") });
  const act = useMutation({
    mutationFn: (a: string) => post(`/api/jobs/${jobId}/${a}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["job", jobId] }); qc.invalidateQueries({ queryKey: ["live"] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const prio = useMutation({
    mutationFn: (p: number) => patch(`/api/jobs/${jobId}`, { priority: p }),
    onSuccess: () => { toast("Priorität geändert", "ok"); qc.invalidateQueries({ queryKey: ["job", jobId] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  if (job.isLoading) return <Loading />;
  if (job.error || !job.data) return <ErrorBox error={job.error} />;
  const j = job.data;
  const lj = live.data?.jobs.find((x) => x.id === jobId);
  const active = ["queued", "running", "paused"].includes(j.state);
  const exp = (f: string) => `/api/export?format=${f}&scope=job&job_id=${j.id}`;
  return (
    <div className="space-y-4">
      <Link to="/scans" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-sky-500"><ArrowLeft className="h-4 w-4" />Zu den Scans</Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Scan #{j.id}: {j.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">
            <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATE_CLS[j.state] ?? ""}`}>{JOB_STATE_LABELS[j.state] ?? j.state}</span>
            <MethodChips methods={j.methods} />
            <span className="text-xs text-slate-500">{j.task_label}</span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {(j.state === "paused" || j.state === "interrupted") && <button className="btn-primary" onClick={() => act.mutate("resume")}><Play className="h-4 w-4" />Fortsetzen</button>}
          {(j.state === "running" || j.state === "queued") && <button className="btn-secondary" onClick={() => act.mutate("pause")}><Pause className="h-4 w-4" />Pausieren</button>}
          {(active || j.state === "interrupted") && <button className="btn-secondary text-red-500" onClick={() => confirm("Scan abbrechen?") && act.mutate("cancel")}><Square className="h-4 w-4" />Abbrechen</button>}
          <label className="flex items-center gap-1 text-xs text-slate-500">Priorität
            <input type="number" className="input w-20" defaultValue={j.priority} min={-100} max={100} onBlur={(e) => Number(e.target.value) !== j.priority && prio.mutate(Number(e.target.value))} />
          </label>
          <a className="btn-secondary" href={exp("csv")}><Download className="h-4 w-4" />CSV</a>
          <a className="btn-secondary" href={exp("html")} target="_blank" rel="noreferrer"><Download className="h-4 w-4" />HTML</a>
          <a className="btn-secondary" href={exp("json")}><Download className="h-4 w-4" />JSON</a>
        </div>
      </div>

      {lj && <LiveJobCard job={lj} />}

      <Card title="Übersicht">
        <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4 xl:grid-cols-6">
          <div><dt className="text-xs text-slate-500">Dateien gesamt</dt><dd className="tabular-nums">{fmtInt(j.total_files)}</dd></div>
          <div><dt className="text-xs text-slate-500">Geprüft</dt><dd className="tabular-nums">{fmtInt(j.done_files)}</dd></div>
          <div><dt className="text-xs text-slate-500">Übersprungen</dt><dd className="tabular-nums">{fmtInt(j.skipped_files)}</dd></div>
          <div><dt className="text-xs text-slate-500">Fehlgeschlagen</dt><dd className="tabular-nums">{fmtInt(j.failed_files)}</dd></div>
          <div><dt className="text-xs text-slate-500">Datenmenge</dt><dd className="tabular-nums">{fmtBytes(j.done_bytes)} / {fmtBytes(j.total_bytes)}</dd></div>
          <div><dt className="text-xs text-slate-500">Aktive Prüfzeit</dt><dd className="tabular-nums">{fmtEta(j.active_seconds)}</dd></div>
          <div><dt className="text-xs text-slate-500">Mit Fehlern</dt><dd className="tabular-nums text-orange-500">{fmtInt(j.files_with_errors)}</dd></div>
          <div><dt className="text-xs text-slate-500">Mit Warnungen</dt><dd className="tabular-nums text-amber-500">{fmtInt(j.files_with_warnings)}</dd></div>
          <div><dt className="text-xs text-slate-500">Befunde</dt><dd className="tabular-nums">{fmtInt(j.findings_count)}</dd></div>
          <div><dt className="text-xs text-slate-500">Erstellt</dt><dd>{fmtDate(j.created_at)}</dd></div>
          <div><dt className="text-xs text-slate-500">Gestartet</dt><dd>{fmtDate(j.started_at)}</dd></div>
          <div><dt className="text-xs text-slate-500">Beendet</dt><dd>{fmtDate(j.finished_at)}</dd></div>
        </dl>
        {j.message && <p className="mt-3 text-sm text-slate-500">{j.message}</p>}
      </Card>

      <div className="grid gap-4 xl:grid-cols-5">
        <Card title="Dateien des Auftrags" className="xl:col-span-3" actions={
          <select className="input w-auto" value={state} onChange={(e) => { setState(e.target.value); setPage(1); }}>
            <option value="">Alle ({fmtInt(Object.values(j.item_counts).reduce((a, b) => a + b, 0))})</option>
            {Object.entries(ITEM_STATES).map(([k, v]) => <option key={k} value={k}>{v} ({fmtInt(j.item_counts[k] ?? 0)})</option>)}
          </select>}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px]">
              <thead><tr><th className="th">Datei</th><th className="th">Ablauf</th><th className="th">Ergebnis</th><th className="th">Größe</th><th className="th">Beendet</th></tr></thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {items.data?.items.map((it) => (
                  <tr key={it.id}>
                    <td className="td"><Link className="font-medium hover:text-sky-500 [overflow-wrap:anywhere]" to={`/dateien/${it.file_id}`}>{it.filename}</Link><div className="text-[11px] text-slate-500">{it.directory}</div>{it.message && <div className="text-[11px] text-slate-500">{it.message}</div>}</td>
                    <td className="td text-xs">{ITEM_STATES[it.state] ?? it.state}{it.attempts > 1 && ` (${it.attempts}. Versuch)`}</td>
                    <td className="td">{it.status ? <StatusBadge status={it.status} small /> : "–"}</td>
                    <td className="td text-xs tabular-nums">{fmtBytes(it.size)}</td>
                    <td className="td text-xs">{fmtDate(it.finished_at, true)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {items.data?.items.length === 0 && <Empty>Keine Einträge.</Empty>}
          <div className="mt-3"><Pagination page={page} pageSize={50} total={items.data?.total ?? 0} onPage={setPage} /></div>
        </Card>
        <Card title="Protokoll" className="xl:col-span-2">
          <ol className="space-y-2 text-sm">
            {j.events.map((e, i) => (
              <li key={i} className="border-l-2 pl-3" style={{ borderColor: e.level === "error" ? "#ef4444" : e.level === "warning" ? "#fbbf24" : "#334155" }}>
                <div className="text-[11px] text-slate-500">{fmtDate(e.ts)}</div>
                <div className="[overflow-wrap:anywhere]">{e.message}</div>
              </li>
            ))}
          </ol>
        </Card>
      </div>
    </div>
  );
}
