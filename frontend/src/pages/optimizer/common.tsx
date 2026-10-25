import { KeyboardEvent, ReactNode, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Search, Square, X } from "lucide-react";
import { api, post, qs } from "../../api";
import { fmtBytes, fmtInt } from "../../format";
import type { AnalyzeState, Choice, OptPlan, OptStatus, OptTrack } from "../../types";
import { ErrorBox, Modal, ProgressBar, Spinner } from "../../components/ui";
import { useToast } from "../../components/toast";

export interface Folder {
  library_id: number | null;
  directory: string;
  recursive: boolean;
}

export const useOptStatus = () => useQuery({ queryKey: ["opt-status"], queryFn: () => api<OptStatus>("/api/optimizer/status") });

/** Ordnerwahl: Bibliothek + Unterordner (Pfad im Container), Unterordner einbeziehen. */
export function FolderPicker({ value, onChange, children }: { value: Folder; onChange: (f: Folder) => void; children?: ReactNode }) {
  const st = useOptStatus();
  const [browse, setBrowse] = useState(false);
  const libs = st.data?.libraries ?? [];
  const lib = libs.find((l) => l.id === value.library_id) ?? null;
  useEffect(() => {
    if (value.library_id == null && libs.length) onChange({ ...value, library_id: libs[0].id });
  }, [libs, value, onChange]);
  return (
    <div className="flex flex-wrap items-end gap-2">
      <div>
        <label className="label">Bibliothek</label>
        <select className="input w-auto" value={value.library_id ?? ""} onChange={(e) => onChange({ ...value, library_id: Number(e.target.value), directory: "" })}>
          {libs.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
        </select>
      </div>
      <div className="min-w-[260px] flex-1">
        <label className="label">Ordner (relativ zur Bibliothek, leer = gesamte Bibliothek)</label>
        <div className="flex gap-1">
          <input className="input font-mono" value={value.directory} placeholder="z. B. Filme/Animation"
            onChange={(e) => onChange({ ...value, directory: e.target.value })}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); const t = e.dataTransfer.getData("text/plain"); if (t) onChange({ ...value, directory: toRelative(t, lib?.path) }); }} />
          <button type="button" className="btn-secondary" title="Ordner auswählen" onClick={() => setBrowse(true)} disabled={!lib}>…</button>
        </div>
      </div>
      <label className="flex items-center gap-1.5 pb-2 text-sm"><input type="checkbox" checked={value.recursive} onChange={(e) => onChange({ ...value, recursive: e.target.checked })} />Unterordner durchsuchen</label>
      {children}
      {browse && lib && (
        <BrowseModal root={lib.path} start={value.directory} onClose={() => setBrowse(false)}
          onPick={(rel) => { onChange({ ...value, directory: rel }); setBrowse(false); }} />
      )}
    </div>
  );
}

function toRelative(p: string, root?: string): string {
  let s = p.trim();
  if (root && s.startsWith(root)) s = s.slice(root.length);
  return s.replace(/^\/+|\/+$/g, "");
}

interface Browse { path: string; parent: string | null; directories: { name: string; path: string }[] }

function BrowseModal({ root, start, onClose, onPick }: { root: string; start: string; onClose: () => void; onPick: (rel: string) => void }) {
  const [path, setPath] = useState(start ? `${root}/${start}` : root);
  const b = useQuery({ queryKey: ["browse", path], queryFn: () => api<Browse>(`/api/libraries/browse${qs({ path })}`) });
  const rel = toRelative(b.data?.path ?? path, root);
  return (
    <Modal open title="Ordner auswählen" onClose={onClose}
      footer={<><button className="btn-secondary" onClick={onClose}>Abbrechen</button><button className="btn-primary" onClick={() => onPick(rel)}>Diesen Ordner wählen</button></>}>
      <div className="mb-2 font-mono text-xs [overflow-wrap:anywhere]">{b.data?.path ?? path}</div>
      <ul className="max-h-72 overflow-y-auto rounded border border-slate-200 text-sm dark:border-slate-700">
        {b.data?.parent && (b.data.path !== root) && (
          <li><button className="w-full px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => setPath(b.data!.parent!)}>↑ übergeordneter Ordner</button></li>
        )}
        {b.data?.directories.map((d) => (
          <li key={d.path}><button className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => setPath(d.path)}><FolderOpen className="h-4 w-4 text-sky-500" />{d.name}</button></li>
        ))}
      </ul>
      <ErrorBox error={b.error} />
    </Modal>
  );
}

/** Startet die Analyse (mkvmerge -J) und zeigt den Fortschritt. */
export function SearchButton({ folder, label = "Suchen" }: { folder: Folder; label?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const state = useQuery({
    queryKey: ["opt-analyze"], queryFn: () => api<AnalyzeState>("/api/optimizer/analyze"),
    refetchInterval: (q) => (q.state.data?.running ? 1000 : false),
  });
  const start = useMutation({
    mutationFn: (force: boolean) => post<AnalyzeState>("/api/optimizer/analyze", { library_id: folder.library_id, directory: folder.directory, recursive: folder.recursive, force }),
    onSuccess: (d) => { qc.setQueryData(["opt-analyze"], d); qc.invalidateQueries({ queryKey: ["opt-analyze"] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const cancel = useMutation({ mutationFn: () => post("/api/optimizer/analyze/cancel") });
  const s = state.data;
  const running = !!s?.running;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button className="btn-primary" disabled={running || folder.library_id == null || start.isPending} onClick={() => start.mutate(false)}>
        {running ? <Spinner /> : <Search className="h-4 w-4" />}{label}
      </button>
      <button className="btn-ghost text-xs" disabled={running} onClick={() => start.mutate(true)} title="Alle Dateien neu mit mkvmerge analysieren">neu einlesen</button>
      {running && (
        <div className="flex min-w-[220px] items-center gap-2 text-xs text-slate-500">
          <div className="w-40"><ProgressBar value={s!.total ? s!.done / s!.total : 0} /></div>
          {fmtInt(s!.done)} / {fmtInt(s!.total)}
          <button className="btn-ghost p-1" onClick={() => cancel.mutate()} title="Analyse abbrechen"><Square className="h-3.5 w-3.5" /></button>
        </div>
      )}
      {!running && s?.message && <span className="text-xs text-slate-500">{s.message}</span>}
    </div>
  );
}

/** Eingabe einer Liste (Sprachen, Codecs) als entfernbare Chips mit Vorschlägen. */
export function TagInput({ values, onChange, suggestions = [], placeholder, upper }: {
  values: string[]; onChange: (v: string[]) => void; suggestions?: Choice[]; placeholder?: string; upper?: boolean;
}) {
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const parts = raw.split(/[,\s]+/).map((x) => (upper ? x.toUpperCase() : x.toLowerCase()).trim()).filter(Boolean);
    const next = [...values];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    onChange(next);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); if (text.trim()) add(text); }
    if (e.key === "Backspace" && !text && values.length) onChange(values.slice(0, -1));
  };
  const label = (v: string) => suggestions.find((s) => s.value === v)?.label;
  const missing = suggestions.filter((s) => !values.includes(s.value));
  return (
    <div>
      <div className="input flex min-h-[38px] flex-wrap items-center gap-1 py-1">
        {values.map((v) => (
          <span key={v} className="inline-flex items-center gap-1 rounded bg-sky-500/15 px-1.5 py-0.5 text-xs font-medium text-sky-700 dark:text-sky-300">
            {upper ? v : v.toUpperCase()}{label(v) && !upper ? <span className="font-normal opacity-70">{label(v)}</span> : null}
            <button type="button" onClick={() => onChange(values.filter((x) => x !== v))} aria-label={`${v} entfernen`}><X className="h-3 w-3" /></button>
          </span>
        ))}
        <input className="min-w-[80px] flex-1 bg-transparent text-sm outline-none" value={text} placeholder={values.length ? "" : placeholder}
          onChange={(e) => setText(e.target.value)} onKeyDown={onKey} onBlur={() => text.trim() && add(text)} />
      </div>
      {missing.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {missing.slice(0, 20).map((s) => (
            <button key={s.value} type="button" className="rounded border border-dashed border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-500 hover:border-sky-500 hover:text-sky-500 dark:border-slate-600"
              onClick={() => onChange([...values, s.value])}>+ {upper ? s.value : `${s.value.toUpperCase()} ${s.label}`} ({fmtInt(s.count)})</button>
          ))}
        </div>
      )}
    </div>
  );
}

export function trackText(t: OptTrack): string {
  const ch = t.channels ? ({ 1: " 1.0", 2: " 2.0", 6: " 5.1", 8: " 7.1" } as Record<number, string>)[t.channels] ?? ` ${t.channels}ch` : "";
  return `${t.language.toUpperCase()} (${t.family ?? t.codec ?? "?"}${ch})`;
}

/** Spur als Chip: grün = bleibt, rot durchgestrichen = wird entfernt, blau = wird konvertiert. */
export function TrackChip({ t, keep, convert, title }: { t: OptTrack; keep?: boolean; convert?: boolean; title?: string }) {
  const cls = convert
    ? "bg-sky-500/15 text-sky-700 ring-sky-500/30 dark:text-sky-300"
    : keep === false
      ? "bg-red-500/10 text-red-600 line-through ring-red-500/30 dark:text-red-400"
      : keep === true
        ? "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-400"
        : "bg-slate-500/10 text-slate-600 ring-slate-500/20 dark:text-slate-300";
  return (
    <span title={title ?? `Track-ID ${t.id}${t.name ? ` – ${t.name}` : ""}${t.default ? " – Standard" : ""}`}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${cls}`}>
      <span className="opacity-60">#{t.id}</span>{trackText(t)}{t.default && <span className="opacity-60">★</span>}{t.forced && <span className="opacity-60">F</span>}
    </span>
  );
}

export function planBadge(plan: OptPlan | null, analyzed: boolean, ok: boolean | null) {
  if (!analyzed) return <span className="text-xs text-slate-500">nicht analysiert – „Suchen“</span>;
  if (ok === false) return <span className="text-xs text-red-500">nicht lesbar</span>;
  if (!plan) return null;
  if (plan.changed) return <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">wird bearbeitet</span>;
  return <span className="text-xs text-slate-500">{plan.skip_reason}</span>;
}

export interface CommandInfo { plan: OptPlan; output_mode: string; destination: string; commands: string[]; tracks: OptTrack[] }

/** Detailansicht einer Datei (ersetzt den Doppelklick auf die mkvmerge-GUI). */
export function CommandModal({ fileId, body, onClose }: { fileId: number; body: Record<string, unknown>; onClose: () => void }) {
  const q = useQuery({ queryKey: ["opt-command", fileId, body], queryFn: () => post<CommandInfo>("/api/optimizer/command", { file_id: fileId, ...body }) });
  const d = q.data;
  const decisions = new Map((d?.plan.decisions ?? []).map((x) => [x.track_id, x]));
  const conv = new Map((d?.plan.conversions ?? []).map((x) => [x.track_id, x]));
  return (
    <Modal open wide title="Spuren und Befehle" onClose={onClose}>
      {q.isLoading && <Spinner />}
      <ErrorBox error={q.error} />
      {d && (
        <div className="space-y-4 text-sm">
          <table className="w-full">
            <thead><tr><th className="th">ID</th><th className="th">Typ</th><th className="th">Codec</th><th className="th">Sprache</th><th className="th">Name</th><th className="th">Flags</th><th className="th">Aktion</th></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {d.tracks.map((t) => {
                const dec = decisions.get(t.id);
                const c = conv.get(t.id);
                return (
                  <tr key={t.id}>
                    <td className="td tabular-nums">{t.id}</td>
                    <td className="td">{{ video: "Video", audio: "Audio", subtitles: "Untertitel" }[t.type] ?? t.type}</td>
                    <td className="td">{t.codec}{t.channels ? ` · ${t.channels} Kanäle` : ""}</td>
                    <td className="td">{t.language.toUpperCase()} <span className="text-xs text-slate-500">{t.language_label}</span></td>
                    <td className="td text-xs">{t.name ?? "–"}</td>
                    <td className="td text-xs">{t.default ? "Standard " : ""}{t.forced ? "Erzwungen" : ""}</td>
                    <td className="td text-xs">
                      {c ? <span className="text-sky-500">→ {c.track_name} ({d.plan.mode === "add" ? "zusätzlich" : "ersetzt"})</span>
                        : dec ? <span className={dec.keep ? "text-emerald-500" : "text-red-500"}>{dec.keep ? "behalten" : "entfernen"} – {dec.reason}</span> : "–"}
                      {d.plan.default_track === t.id && <div className="text-amber-500">wird Standardspur</div>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {d.plan.skip_reason && <p className="text-slate-500">Keine Bearbeitung: {d.plan.skip_reason}</p>}
          {(d.plan.warnings ?? []).map((w) => <p key={w} className="text-xs text-amber-500">{w}</p>)}
          <div><div className="label">Ziel</div><code className="text-xs [overflow-wrap:anywhere]">{d.destination}</code></div>
          {d.commands.length > 0 && (
            <div>
              <div className="label">Befehle</div>
              {d.commands.map((c, i) => <pre key={i} className="mb-2 overflow-x-auto whitespace-pre-wrap rounded bg-slate-950 p-2 font-mono text-[11px] text-slate-200">{c}</pre>)}
              <p className="text-xs text-slate-500">Die neue Datei wird erst in eine temporäre Datei geschrieben und geprüft (Spuren, Laufzeit, Struktur), bevor sie abgeschlossen wird.</p>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** Kleines Kontextmenü (Rechtsklick in der Dateiliste). */
export function ContextMenu({ pos, items, onClose }: { pos: { x: number; y: number } | null; items: { label: string; action: () => void }[]; onClose: () => void }) {
  useEffect(() => {
    if (!pos) return;
    const h = () => onClose();
    window.addEventListener("click", h);
    window.addEventListener("scroll", h, true);
    return () => { window.removeEventListener("click", h); window.removeEventListener("scroll", h, true); };
  }, [pos, onClose]);
  if (!pos) return null;
  return (
    <div className="card fixed z-50 min-w-[220px] py-1 shadow-xl" style={{ left: pos.x, top: pos.y }} role="menu">
      {items.map((it) => (
        <button key={it.label} role="menuitem" className="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => { it.action(); onClose(); }}>{it.label}</button>
      ))}
    </div>
  );
}

export const OUTPUT_MODE_HINT: Record<string, string> = {
  output_dir: "Neue Dateien landen im Ausgabeverzeichnis – Originale bleiben unverändert.",
  beside: "Neue Dateien werden neben dem Original abgelegt (mit Suffix).",
  replace_backup: "Originale werden ersetzt und in den Sicherungsordner verschoben.",
  replace_delete: "Originale werden ersetzt und gelöscht!",
};

/** Vorhersage der Speicheränderung: positiv = Ersparnis, negativ = Datei wird größer. */
export function Saving({ bytes, exact, unknown, compact }: { bytes: number | null | undefined; exact?: boolean; unknown?: number; compact?: boolean }) {
  if (bytes == null) return <span className="text-xs text-slate-500" title="Für die betroffenen Spuren liegen keine Größenangaben vor (keine Statistik-Tags, variable Bitrate)">nicht vorhersagbar</span>;
  const grows = bytes < 0;
  const txt = grows ? `${exact ? "" : "ca. "}+${fmtBytes(-bytes)} größer` : `${exact ? "" : "ca. "}${fmtBytes(bytes)}`;
  const title = exact
    ? "Exakt laut Statistik-Tags der Spuren (Container-Overhead nicht berücksichtigt)"
    : "Geschätzt aus Bitrate × Laufzeit bzw. Statistik-Tags" + (unknown ? ` – ${unknown} Spur(en) ohne Größenangabe nicht berücksichtigt` : "");
  return (
    <span className={`whitespace-nowrap text-xs tabular-nums ${grows ? "text-amber-500" : "text-emerald-600 dark:text-emerald-400"}`} title={title}>
      {txt}{!compact && unknown ? <span className="text-slate-500"> (+{unknown} unbek.)</span> : null}
    </span>
  );
}

export function sumPredictions(ids: Iterable<number>, predictions: Record<string, number | null> | undefined): { bytes: number; unknown: number } {
  let bytes = 0;
  let unknown = 0;
  for (const id of ids) {
    const v = predictions?.[String(id)];
    if (v === undefined) continue;
    if (v == null) unknown++;
    else bytes += v;
  }
  return { bytes, unknown };
}
