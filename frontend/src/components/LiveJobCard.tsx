import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, SkipForward, Square } from "lucide-react";
import { post } from "../api";
import { fmtBytes, fmtEta, fmtInt, fmtPct, fmtSpeed, JOB_STATE_LABELS, METHOD_LABELS } from "../format";
import type { LiveJob } from "../types";
import { MethodChips, ProgressBar } from "./ui";
import { useToast } from "./toast";

export function LiveJobCard({ job, compact }: { job: LiveJob; compact?: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const act = useMutation({
    mutationFn: (a: "pause" | "resume" | "cancel") => post(`/api/jobs/${job.id}/${a}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["live"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const skip = useMutation({
    mutationFn: (fileId: number) => post(`/api/jobs/${job.id}/skip?file_id=${fileId}`),
    onSuccess: () => {
      toast("Datei wird übersprungen", "ok");
      qc.invalidateQueries({ queryKey: ["live"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const total = job.total_bytes || 0;
  const frac = total > 0 ? job.processed_bytes / total : job.total_files ? (job.done_files + job.skipped_files) / job.total_files : 0;
  const preparing = job.phase === "prepare";
  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to={`/scans/${job.id}`} className="font-medium hover:text-sky-500">#{job.id} {job.name}</Link>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span className={`rounded-full px-2 py-0.5 font-medium ${job.state === "paused" ? "bg-amber-400/20 text-amber-600 dark:text-amber-300" : "bg-sky-500/15 text-sky-600 dark:text-sky-300"}`}>
              {preparing ? "Bestandsaufnahme …" : job.pausing_after_file ? "Pausiert nach aktueller Datei …" : JOB_STATE_LABELS[job.state] ?? job.state}
            </span>
            <MethodChips methods={job.methods} />
            {job.priority !== 0 && <span>Priorität {job.priority}</span>}
          </div>
        </div>
        <div className="flex gap-1">
          {job.state === "paused" ? (
            <button className="btn-secondary" onClick={() => act.mutate("resume")} disabled={act.isPending}><Play className="h-4 w-4" />Fortsetzen</button>
          ) : (
            <button className="btn-secondary" onClick={() => act.mutate("pause")} disabled={act.isPending}><Pause className="h-4 w-4" />Pausieren</button>
          )}
          <button className="btn-secondary text-red-600 dark:text-red-400" onClick={() => { if (confirm("Scan wirklich abbrechen?")) act.mutate("cancel"); }} disabled={act.isPending}>
            <Square className="h-4 w-4" />Abbrechen
          </button>
        </div>
      </div>
      <div className="mt-3 space-y-1">
        <div className="flex justify-between text-xs text-slate-500">
          <span>Gesamtfortschritt</span>
          <span className="tabular-nums">{fmtPct(frac)} · {fmtBytes(job.processed_bytes)} von {fmtBytes(total)}</span>
        </div>
        <ProgressBar value={frac} striped={preparing} />
      </div>
      {!compact && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <div><dt className="text-slate-500">Dateien</dt><dd className="tabular-nums">{fmtInt(job.done_files + job.skipped_files)} / {fmtInt(job.total_files)}</dd></div>
          <div><dt className="text-slate-500">Geschwindigkeit</dt><dd className="tabular-nums">{fmtSpeed(job.speed_bps)}</dd></div>
          <div><dt className="text-slate-500">Restdauer</dt><dd className="tabular-nums">{job.state === "paused" ? "pausiert" : fmtEta(job.eta_seconds)}</dd></div>
          <div><dt className="text-slate-500">Parallel</dt><dd className="tabular-nums">{job.parallel}</dd></div>
          <div><dt className="text-slate-500">Dateien mit Fehlern</dt><dd className="tabular-nums text-orange-500">{fmtInt(job.files_with_errors)}</dd></div>
          <div><dt className="text-slate-500">Mit Warnungen</dt><dd className="tabular-nums text-amber-500">{fmtInt(job.files_with_warnings)}</dd></div>
          <div><dt className="text-slate-500">Befunde</dt><dd className="tabular-nums">{fmtInt(job.findings_count)}</dd></div>
          <div><dt className="text-slate-500">Übersprungen</dt><dd className="tabular-nums">{fmtInt(job.skipped_files)}</dd></div>
        </dl>
      )}
      {job.current.length > 0 && (
        <ul className="mt-3 space-y-2">
          {job.current.map((c) => (
            <li key={c.file_id} className="rounded-md bg-slate-50 p-2 dark:bg-slate-800/60">
              <div className="flex justify-between gap-2 text-xs">
                <Link to={`/dateien/${c.file_id}`} className="truncate font-medium hover:text-sky-500" title={c.filename ?? ""}>{c.filename ?? `Datei ${c.file_id}`}</Link>
                <span className="shrink-0 tabular-nums text-slate-500">
                  {c.paused ? "angehalten · " : ""}{c.method ? (METHOD_LABELS[c.method] ?? (c.method === "probe" ? "Analyse" : c.method)) : "Start"} · {fmtPct(c.fraction)} · {fmtBytes(c.size)}
                  <button className="ml-2 inline-flex items-center gap-0.5 rounded px-1 text-slate-500 hover:bg-slate-200 hover:text-sky-500 dark:hover:bg-slate-700"
                    title="Prüfung dieser Datei abbrechen und mit der nächsten weitermachen" disabled={skip.isPending}
                    onClick={() => { if (confirm(`„${c.filename ?? c.file_id}“ überspringen?`)) skip.mutate(c.file_id); }}>
                    <SkipForward className="h-3.5 w-3.5" />Überspringen
                  </button>
                </span>
              </div>
              <div className="mt-1"><ProgressBar value={c.fraction} tone="emerald" /></div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
