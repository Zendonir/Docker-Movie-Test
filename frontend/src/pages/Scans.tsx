import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { api, ApiError, post, qs } from "../api";
import { fmtBytes, fmtDate, fmtInt, JOB_STATE_LABELS } from "../format";
import type { Job, Library, LiveState, Method, Paged, Settings } from "../types";
import { Card, Empty, Loading, MethodChips, Pagination } from "../components/ui";
import { LiveJobCard } from "../components/LiveJobCard";
import { MethodPicker } from "./Files";
import { useToast } from "../components/toast";

const TASKS = [
  { value: "incremental", label: "Nur neue und geänderte Dateien (inkrementell)", hint: "Unveränderte, mit denselben Methoden vollständig geprüfte Dateien werden übersprungen." },
  { value: "new", label: "Nur neue Dateien", hint: "Dateien, die noch nie geprüft wurden." },
  { value: "changed", label: "Nur geänderte Dateien", hint: "Dateien, deren Größe oder Änderungszeit sich geändert hat." },
  { value: "errors", label: "Fehlerhafte Dateien erneut prüfen", hint: "Kritisch, Fehler und Warnung – unabhängig von früheren Ergebnissen." },
  { value: "full", label: "Vollständige Bibliothek erneut prüfen", hint: "Alle Dateien, auch bereits geprüfte." },
];

export const STATE_CLS: Record<string, string> = {
  queued: "bg-slate-500/15 text-slate-500", running: "bg-sky-500/15 text-sky-500", paused: "bg-amber-400/20 text-amber-500",
  completed: "bg-emerald-500/15 text-emerald-500", cancelled: "bg-slate-500/15 text-slate-400", failed: "bg-red-500/15 text-red-500",
  interrupted: "bg-violet-500/15 text-violet-400",
};

function NewScan({ onCreated }: { onCreated: () => void }) {
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/api/libraries") });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("/api/settings") });
  const [task, setTask] = useState("incremental");
  const [methods, setMethods] = useState<Method[] | null>(null);
  const [libIds, setLibIds] = useState<number[]>([]);
  const [priority, setPriority] = useState(0);
  const toast = useToast();
  const ms = methods ?? (settings.data?.default_methods as Method[] | undefined) ?? ["quick", "container", "full"];
  const create = useMutation({
    mutationFn: () => post<Job>("/api/jobs", { task, methods: ms, library_ids: libIds.length ? libIds : null, priority }),
    onSuccess: (j) => { toast(`Scan #${j.id} gestartet`, "ok"); onCreated(); },
    onError: (e: Error) => {
      const d = e instanceof ApiError ? (e.detail as { job_id?: number }) : undefined;
      toast(d?.job_id ? `Ein identischer Scan ist bereits aktiv (#${d.job_id}).` : e.message, "error");
    },
  });
  const toggle = (m: Method) => setMethods(ms.includes(m) ? ms.filter((x) => x !== m) : [...ms, m]);
  return (
    <Card title="Neuen Scan starten">
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <label className="label">Aufgabe</label>
            <div className="space-y-1.5">
              {TASKS.map((t) => (
                <label key={t.value} className="flex cursor-pointer items-start gap-2 text-sm">
                  <input type="radio" name="task" className="mt-1" checked={task === t.value} onChange={() => setTask(t.value)} />
                  <span><span className="font-medium">{t.label}</span><span className="block text-xs text-slate-500">{t.hint}</span></span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <label className="label">Bibliotheken</label>
            <div className="flex flex-wrap gap-2">
              {libs.data?.map((l) => (
                <label key={l.id} className="flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 text-sm dark:border-slate-700">
                  <input type="checkbox" checked={libIds.includes(l.id)} onChange={() => setLibIds(libIds.includes(l.id) ? libIds.filter((x) => x !== l.id) : [...libIds, l.id])} />
                  {l.name}
                </label>
              ))}
              {libs.data?.length === 0 && <span className="text-sm text-slate-500">Keine Bibliothek – bitte unter „Bibliotheken“ anlegen.</span>}
            </div>
            <p className="mt-1 text-xs text-slate-500">Ohne Auswahl werden alle aktiven Bibliotheken geprüft.</p>
          </div>
          <div>
            <label className="label">Priorität</label>
            <input type="number" min={-100} max={100} className="input w-28" value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
          </div>
        </div>
        <div className="space-y-3">
          <label className="label">Prüfmethoden</label>
          <MethodPicker methods={ms} toggle={toggle} />
          <button className="btn-primary" disabled={!ms.length || create.isPending} onClick={() => create.mutate()}><Play className="h-4 w-4" />Scan starten</button>
        </div>
      </div>
    </Card>
  );
}

export function Scans() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const live = useQuery({ queryKey: ["live"], queryFn: () => api<LiveState>("/api/scan/live") });
  const jobs = useQuery({ queryKey: ["jobs", page], queryFn: () => api<Paged<Job>>(`/api/jobs${qs({ page, page_size: 20 })}`) });
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Scans</h1>
        <p className="text-sm text-slate-500">Aufträge starten, pausieren, fortsetzen und den Verlauf einsehen.</p>
      </div>
      <Card title={`Aktive Scans (${live.data?.jobs.length ?? 0})`}>
        {live.isLoading ? <Loading /> : live.data?.jobs.length ? (
          <div className="space-y-3">{live.data.jobs.map((j) => <LiveJobCard key={j.id} job={j} />)}</div>
        ) : <Empty>Derzeit läuft kein Scan.</Empty>}
      </Card>
      <NewScan onCreated={() => { qc.invalidateQueries({ queryKey: ["live"] }); qc.invalidateQueries({ queryKey: ["jobs"] }); }} />
      <Card title="Scanverlauf">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px]">
            <thead><tr><th className="th">#</th><th className="th">Auftrag</th><th className="th">Status</th><th className="th">Methoden</th><th className="th">Dateien</th><th className="th">Daten</th><th className="th">Ergebnis</th><th className="th">Erstellt</th><th className="th">Beendet</th></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {jobs.data?.items.map((j) => (
                <tr key={j.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className="td tabular-nums"><Link className="hover:text-sky-500" to={`/scans/${j.id}`}>{j.id}</Link></td>
                  <td className="td"><Link className="font-medium hover:text-sky-500" to={`/scans/${j.id}`}>{j.name}</Link><div className="text-[11px] text-slate-500">{j.trigger === "schedule" ? "Zeitplan" : "manuell"}{j.resume_count ? ` · ${j.resume_count}× fortgesetzt` : ""}</div></td>
                  <td className="td"><span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATE_CLS[j.state] ?? ""}`}>{JOB_STATE_LABELS[j.state] ?? j.state}</span></td>
                  <td className="td"><MethodChips methods={j.methods} /></td>
                  <td className="td text-xs tabular-nums">{fmtInt(j.done_files)} geprüft<br />{fmtInt(j.skipped_files)} übersprungen{j.failed_files ? <><br /><span className="text-red-500">{j.failed_files} fehlgeschl.</span></> : null}</td>
                  <td className="td text-xs tabular-nums">{fmtBytes(j.done_bytes)} / {fmtBytes(j.total_bytes)}</td>
                  <td className="td text-xs tabular-nums"><span className="text-orange-500">{fmtInt(j.files_with_errors)} Fehler</span> · <span className="text-amber-500">{fmtInt(j.files_with_warnings)} Warn.</span></td>
                  <td className="td text-xs">{fmtDate(j.created_at, true)}</td>
                  <td className="td text-xs">{fmtDate(j.finished_at, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {jobs.data && jobs.data.items.length === 0 && <Empty>Noch keine Scans.</Empty>}
        <div className="mt-3"><Pagination page={page} pageSize={20} total={jobs.data?.total ?? 0} onPage={setPage} /></div>
      </Card>
    </div>
  );
}
