import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Play, Plus, Trash2 } from "lucide-react";
import { api, del, post, put } from "../api";
import { fmtDate } from "../format";
import type { Library, Method, Schedule } from "../types";
import { Card, Empty, ErrorBox, Loading, MethodChips, Modal, Toggle } from "../components/ui";
import { MethodPicker } from "./Files";
import { useToast } from "../components/toast";

const KINDS = { daily: "Täglich", weekly: "Wöchentlich", monthly: "Monatlich", custom: "Benutzerdefiniert (Cron)" };
const TASKS = { new_changed: "Neue und geänderte Dateien prüfen", new: "Neue Dateien prüfen", changed: "Geänderte Dateien prüfen", full: "Vollständige Bibliothek erneut prüfen", errors: "Bereits fehlerhafte Dateien nachprüfen" };
const WEEKDAYS = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];

interface Form { name: string; enabled: boolean; kind: keyof typeof KINDS; time: string; weekday: number; day: number; cron: string; task: keyof typeof TASKS; methods: Method[]; library_ids: number[]; priority: number }

function fromSchedule(s?: Schedule): Form {
  const base: Form = { name: "", enabled: true, kind: "daily", time: "03:00", weekday: 0, day: 1, cron: "0 3 * * *", task: "new_changed", methods: ["quick", "container", "full"], library_ids: [], priority: 0 };
  if (!s) return base;
  const [mm, hh, dom, , dow] = s.cron.split(" ");
  return { ...base, name: s.name, enabled: s.enabled, kind: s.kind, cron: s.cron, task: s.task as Form["task"], methods: s.methods, library_ids: s.library_ids, priority: s.priority,
    time: /^\d+$/.test(hh) && /^\d+$/.test(mm) ? `${hh.padStart(2, "0")}:${mm.padStart(2, "0")}` : "03:00",
    weekday: /^\d$/.test(dow) ? Number(dow) : 0, day: /^\d+$/.test(dom) ? Number(dom) : 1 };
}

function ScheduleForm({ initial, id, onClose }: { initial?: Schedule; id?: number; onClose: () => void }) {
  const [f, setF] = useState<Form>(fromSchedule(initial));
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/api/libraries") });
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => {
      const body = { name: f.name, enabled: f.enabled, kind: f.kind, time: f.kind === "custom" ? null : f.time, weekday: f.kind === "weekly" ? f.weekday : null, day: f.kind === "monthly" ? f.day : null, cron: f.kind === "custom" ? f.cron : null, task: f.task, methods: f.methods, library_ids: f.library_ids, priority: f.priority };
      return id ? put(`/api/schedules/${id}`, body) : post("/api/schedules", body);
    },
    onSuccess: () => { toast("Zeitplan gespeichert", "ok"); qc.invalidateQueries({ queryKey: ["schedules"] }); onClose(); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  return (
    <Modal open wide title={id ? "Zeitplan bearbeiten" : "Neuer Zeitplan"} onClose={onClose}
      footer={<><button className="btn-secondary" onClick={onClose}>Abbrechen</button><button className="btn-primary" disabled={!f.name || !f.methods.length || save.isPending} onClick={() => save.mutate()}>Speichern</button></>}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <div><label className="label">Name</label><input className="input" value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="z. B. Nächtliche Prüfung" /></div>
          <div><label className="label">Häufigkeit</label>
            <select className="input" value={f.kind} onChange={(e) => set("kind", e.target.value as Form["kind"])}>
              {Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </div>
          {f.kind !== "custom" && <div><label className="label">Uhrzeit</label><input type="time" className="input w-36" value={f.time} onChange={(e) => set("time", e.target.value)} /></div>}
          {f.kind === "weekly" && <div><label className="label">Wochentag</label><select className="input" value={f.weekday} onChange={(e) => set("weekday", Number(e.target.value))}>{WEEKDAYS.map((w, i) => <option key={i} value={i}>{w}</option>)}</select></div>}
          {f.kind === "monthly" && <div><label className="label">Tag im Monat (1–28)</label><input type="number" min={1} max={28} className="input w-28" value={f.day} onChange={(e) => set("day", Number(e.target.value))} /></div>}
          {f.kind === "custom" && <div><label className="label">Cron-Ausdruck (Minute Stunde Tag Monat Wochentag)</label><input className="input font-mono" value={f.cron} onChange={(e) => set("cron", e.target.value)} /><p className="mt-1 text-xs text-slate-500">Beispiel: <code>30 2 * * 1-5</code> = werktags 02:30. Zeitzone laut TZ-Variable.</p></div>}
          <div><label className="label">Aufgabe</label><select className="input" value={f.task} onChange={(e) => set("task", e.target.value as Form["task"])}>{Object.entries(TASKS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
          <div><label className="label">Bibliotheken (leer = alle)</label>
            <div className="flex flex-wrap gap-2">{libs.data?.map((l) => (
              <label key={l.id} className="flex items-center gap-1.5 rounded border border-slate-200 px-2 py-1 text-sm dark:border-slate-700"><input type="checkbox" checked={f.library_ids.includes(l.id)} onChange={() => set("library_ids", f.library_ids.includes(l.id) ? f.library_ids.filter((x) => x !== l.id) : [...f.library_ids, l.id])} />{l.name}</label>
            ))}</div>
          </div>
          <div><label className="label">Priorität</label><input type="number" className="input w-28" value={f.priority} onChange={(e) => set("priority", Number(e.target.value))} /></div>
          <Toggle checked={f.enabled} onChange={(v) => set("enabled", v)} label="Aktiv" />
        </div>
        <div><label className="label">Prüfmethoden</label><MethodPicker methods={f.methods} toggle={(m) => set("methods", f.methods.includes(m) ? f.methods.filter((x) => x !== m) : [...f.methods, m])} /></div>
      </div>
    </Modal>
  );
}

export function Schedules() {
  const q = useQuery({ queryKey: ["schedules"], queryFn: () => api<Schedule[]>("/api/schedules"), refetchInterval: 30000 });
  const [edit, setEdit] = useState<Schedule | "new" | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const remove = useMutation({ mutationFn: (id: number) => del(`/api/schedules/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }) });
  const run = useMutation({ mutationFn: (id: number) => post<{ job_id: number }>(`/api/schedules/${id}/run`), onSuccess: (r) => toast(`Auftrag #${r.job_id} gestartet`, "ok"), onError: (e: Error) => toast(e.message, "error") });
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div><h1 className="text-xl font-semibold">Zeitpläne</h1><p className="text-sm text-slate-500">Automatische Prüfungen. Identische Aufträge werden nie doppelt gestartet; verpasste Termine werden nach einem Neustart höchstens einmal nachgeholt.</p></div>
        <button className="btn-primary" onClick={() => setEdit("new")}><Plus className="h-4 w-4" />Neuer Zeitplan</button>
      </div>
      <Card>
        {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : q.data!.length === 0 ? <Empty>Noch keine Zeitpläne.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px]">
              <thead><tr><th className="th">Name</th><th className="th">Rhythmus</th><th className="th">Aufgabe</th><th className="th">Methoden</th><th className="th">Nächster Lauf</th><th className="th">Letzter Lauf</th><th className="th"></th></tr></thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {q.data!.map((s) => (
                  <tr key={s.id} className={s.enabled ? "" : "opacity-50"}>
                    <td className="td font-medium">{s.name}{!s.enabled && <span className="ml-1 text-xs text-slate-500">(inaktiv)</span>}</td>
                    <td className="td text-sm">{KINDS[s.kind]}<div className="font-mono text-[11px] text-slate-500">{s.cron}</div></td>
                    <td className="td text-sm">{s.task_label}</td>
                    <td className="td"><MethodChips methods={s.methods} /></td>
                    <td className="td text-xs">{fmtDate(s.next_run_at)}</td>
                    <td className="td text-xs">{fmtDate(s.last_run_at)}{s.last_job_id && <> · <Link className="hover:text-sky-500" to={`/scans/${s.last_job_id}`}>#{s.last_job_id}</Link></>}{s.last_message && <div className="text-slate-500">{s.last_message}</div>}</td>
                    <td className="td whitespace-nowrap text-right">
                      <button className="btn-ghost p-1.5" title="Jetzt ausführen" onClick={() => run.mutate(s.id)}><Play className="h-4 w-4" /></button>
                      <button className="btn-ghost p-1.5" title="Bearbeiten" onClick={() => setEdit(s)}><Pencil className="h-4 w-4" /></button>
                      <button className="btn-ghost p-1.5 text-red-500" title="Löschen" onClick={() => confirm(`Zeitplan „${s.name}“ löschen?`) && remove.mutate(s.id)}><Trash2 className="h-4 w-4" /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {edit && <ScheduleForm initial={edit === "new" ? undefined : edit} id={edit === "new" ? undefined : edit.id} onClose={() => setEdit(null)} />}
    </div>
  );
}
