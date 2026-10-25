import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Pause, Play, Square } from "lucide-react";
import { api, post, qs } from "../../api";
import { fmtBytes, fmtDate, fmtEta, fmtInt, fmtPct, fmtSpeed, JOB_STATE_LABELS } from "../../format";
import type { OptJob, OptLiveJob, Paged } from "../../types";
import { Card, Empty, Modal, Pagination, ProgressBar } from "../../components/ui";
import { useToast } from "../../components/toast";
import { STATE_CLS } from "../Scans";

const ITEM_STATES: Record<string, string> = { pending: "Ausstehend", running: "Läuft", done: "Erledigt", skipped: "Übersprungen", failed: "Fehlgeschlagen", cancelled: "Abgebrochen" };
const ITEM_CLS: Record<string, string> = { done: "text-emerald-500", failed: "text-red-500", skipped: "text-slate-500", cancelled: "text-slate-500", running: "text-sky-500", pending: "text-slate-400" };

interface Item { id: number; file_id: number | null; name: string; source_path: string; state: string; size: number; output_path: string | null; output_size: number | null; backup_path: string | null; message: string | null; duration_s: number | null; attempts: number }
interface JobDetail extends OptJob { item_counts: Record<string, number>; events: { ts: string; level: string; message: string }[] }

function Actions({ job }: { job: { id: number; state: string } }) {
  const qc = useQueryClient();
  const toast = useToast();
  const act = useMutation({
    mutationFn: (a: string) => post(`/api/optimizer/jobs/${job.id}/${a}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["opt-live"] }); qc.invalidateQueries({ queryKey: ["opt-jobs"] }); qc.invalidateQueries({ queryKey: ["opt-job"] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  if (!["queued", "running", "paused"].includes(job.state)) return null;
  return (
    <div className="flex gap-1">
      {job.state === "paused"
        ? <button className="btn-secondary" onClick={() => act.mutate("resume")}><Play className="h-4 w-4" />Fortsetzen</button>
        : <button className="btn-secondary" onClick={() => act.mutate("pause")}><Pause className="h-4 w-4" />Pausieren</button>}
      <button className="btn-secondary text-red-500" onClick={() => confirm("Auftrag abbrechen? Die laufende Datei wird verworfen, das Original bleibt unverändert.") && act.mutate("cancel")}><Square className="h-4 w-4" />Abbrechen</button>
    </div>
  );
}

function LiveCard({ j }: { j: OptLiveJob }) {
  const frac = j.total_bytes ? j.processed_bytes / j.total_bytes : 0;
  const c = j.current;
  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="font-medium">#{j.id} {j.name}</div>
          <div className="text-xs text-slate-500">{JOB_STATE_LABELS[j.state] ?? j.state} · {fmtInt(j.done_files + j.skipped_files + j.failed_files)} / {fmtInt(j.total_files)} Dateien{j.saved_bytes ? ` · ${fmtBytes(j.saved_bytes)} eingespart` : ""}</div>
        </div>
        <Actions job={j} />
      </div>
      <div className="mt-3 space-y-1">
        <div className="flex justify-between text-xs text-slate-500"><span>Gesamt</span><span className="tabular-nums">{fmtPct(frac)} · {fmtBytes(j.processed_bytes)} / {fmtBytes(j.total_bytes)} · {fmtSpeed(j.speed_bps)} · Restdauer {j.state === "paused" ? "pausiert" : fmtEta(j.eta_seconds)}</span></div>
        <ProgressBar value={frac} />
      </div>
      {c && (
        <div className="mt-3 rounded-md bg-slate-50 p-2 dark:bg-slate-800/60">
          <div className="flex justify-between gap-2 text-xs">
            <span className="truncate font-medium">{c.name}</span>
            <span className="shrink-0 tabular-nums text-slate-500">{c.paused ? "angehalten · " : ""}{c.phase} · {fmtPct(c.fraction)} · {fmtBytes(c.size)} · noch {fmtEta(c.eta_seconds)}</span>
          </div>
          <div className="mt-1"><ProgressBar value={c.fraction} tone="emerald" /></div>
        </div>
      )}
    </div>
  );
}

function JobItems({ jobId }: { jobId: number }) {
  const [state, setState] = useState("");
  const [page, setPage] = useState(1);
  const [log, setLog] = useState<number | null>(null);
  const job = useQuery({ queryKey: ["opt-job", jobId], queryFn: () => api<JobDetail>(`/api/optimizer/jobs/${jobId}`), refetchInterval: 5000 });
  const items = useQuery({ queryKey: ["opt-job", jobId, "items", state, page], queryFn: () => api<Paged<Item>>(`/api/optimizer/jobs/${jobId}/items${qs({ state: state || undefined, page })}`), refetchInterval: 5000 });
  const detail = useQuery({ queryKey: ["opt-item", log], queryFn: () => api<{ log: string | null; plan: unknown; message: string | null }>(`/api/optimizer/items/${log}`), enabled: log != null });
  const j = job.data;
  return (
    <div className="grid gap-4 xl:grid-cols-5">
      <div className="xl:col-span-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">Dateien</span>
          <select className="input w-auto" value={state} onChange={(e) => { setState(e.target.value); setPage(1); }}>
            <option value="">Alle</option>
            {Object.entries(ITEM_STATES).map(([k, v]) => <option key={k} value={k}>{v} ({fmtInt(j?.item_counts[k] ?? 0)})</option>)}
          </select>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[600px]">
            <thead><tr><th className="th">Datei</th><th className="th">Status</th><th className="th">Größe</th><th className="th">Ergebnis</th><th className="th" /></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.data?.items.map((it) => (
                <tr key={it.id}>
                  <td className="td max-w-[320px]">{it.file_id ? <Link className="font-medium hover:text-sky-500 [overflow-wrap:anywhere]" to={`/dateien/${it.file_id}`}>{it.name}</Link> : it.name}
                    {it.output_path && <div className="text-[11px] text-slate-500 [overflow-wrap:anywhere]">→ {it.output_path}</div>}
                    {it.backup_path && <div className="text-[11px] text-slate-500 [overflow-wrap:anywhere]">Sicherung: {it.backup_path}</div>}</td>
                  <td className={`td text-xs ${ITEM_CLS[it.state] ?? ""}`}>{ITEM_STATES[it.state] ?? it.state}{it.attempts > 1 ? ` (${it.attempts}.)` : ""}</td>
                  <td className="td whitespace-nowrap text-xs tabular-nums">{fmtBytes(it.size)}{it.output_size != null && <div className="text-slate-500">→ {fmtBytes(it.output_size)}</div>}</td>
                  <td className="td text-xs">{it.message}{it.duration_s != null && <div className="text-slate-500">{Math.round(it.duration_s)} s</div>}</td>
                  <td className="td"><button className="btn-ghost p-1" title="Protokoll" onClick={() => setLog(it.id)}><FileText className="h-4 w-4" /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2"><Pagination page={page} pageSize={50} total={items.data?.total ?? 0} onPage={setPage} /></div>
      </div>
      <div className="xl:col-span-2">
        <div className="mb-2 text-sm font-medium">Protokoll</div>
        <ol className="max-h-96 space-y-2 overflow-y-auto text-sm">
          {j?.events.map((e, i) => (
            <li key={i} className="border-l-2 pl-3" style={{ borderColor: e.level === "error" ? "#ef4444" : e.level === "warning" ? "#fbbf24" : "#334155" }}>
              <div className="text-[11px] text-slate-500">{fmtDate(e.ts)}</div><div className="[overflow-wrap:anywhere]">{e.message}</div>
            </li>
          ))}
        </ol>
      </div>
      {log != null && (
        <Modal open wide title="Protokoll der Bearbeitung" onClose={() => setLog(null)}>
          {detail.data?.message && <p className="mb-2 text-sm">{detail.data.message}</p>}
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-3 font-mono text-[11px] text-slate-200">{detail.data?.log ?? "…"}</pre>
        </Modal>
      )}
    </div>
  );
}

export function JobsTab() {
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<number | null>(null);
  const live = useQuery({ queryKey: ["opt-live"], queryFn: () => api<{ jobs: OptLiveJob[] }>("/api/optimizer/live"), refetchInterval: 10000 });
  const jobs = useQuery({ queryKey: ["opt-jobs", page], queryFn: () => api<Paged<OptJob>>(`/api/optimizer/jobs${qs({ page })}`), refetchInterval: 10000 });
  return (
    <div className="space-y-4">
      <Card title="Laufende Aufträge">
        {live.data?.jobs.length ? <div className="space-y-3">{live.data.jobs.map((j) => <LiveCard key={j.id} j={j} />)}</div> : <Empty>Kein Optimizer-Auftrag aktiv.</Empty>}
      </Card>
      <Card title="Verlauf">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[800px]">
            <thead><tr><th className="th">#</th><th className="th">Auftrag</th><th className="th">Status</th><th className="th">Dateien</th><th className="th">Eingespart</th><th className="th">Ausgabe</th><th className="th">Erstellt</th><th className="th" /></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {jobs.data?.items.map((j) => (
                <tr key={j.id} className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/40" onClick={() => setOpen(open === j.id ? null : j.id)}>
                  <td className="td tabular-nums">{j.id}</td>
                  <td className="td font-medium">{j.name}<div className="text-[11px] text-slate-500">{j.kind === "remux" ? "Spuren entfernen" : "Audiokonvertierung"}{j.resume_count ? ` · ${j.resume_count}× fortgesetzt` : ""}</div></td>
                  <td className="td"><span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATE_CLS[j.state] ?? ""}`}>{JOB_STATE_LABELS[j.state] ?? j.state}</span></td>
                  <td className="td text-xs tabular-nums">{fmtInt(j.done_files)} erledigt · {fmtInt(j.skipped_files)} übersprungen{j.failed_files ? <span className="text-red-500"> · {j.failed_files} fehlgeschl.</span> : null}</td>
                  <td className="td text-xs tabular-nums">{j.saved_bytes ? fmtBytes(j.saved_bytes) : "–"}</td>
                  <td className="td text-xs">{({ output_dir: "Ausgabeordner", beside: "neben Original", replace_backup: "ersetzt (Sicherung)", replace_delete: "ersetzt" } as Record<string, string>)[j.output_mode] ?? j.output_mode}</td>
                  <td className="td text-xs">{fmtDate(j.created_at, true)}</td>
                  <td className="td" onClick={(e) => e.stopPropagation()}><Actions job={j} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {jobs.data?.items.length === 0 && <Empty>Noch keine Aufträge.</Empty>}
        <div className="mt-3"><Pagination page={page} pageSize={20} total={jobs.data?.total ?? 0} onPage={setPage} /></div>
        {open != null && <div className="mt-4 border-t border-slate-200 pt-4 dark:border-slate-800"><JobItems jobId={open} /></div>}
      </Card>
    </div>
  );
}
