import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Wrench } from "lucide-react";
import { api, post } from "../api";
import { fmtBytes, fmtInt } from "../format";
import { useOptStatus } from "../pages/optimizer/common";
import type { RepairCandidate, TrackRule } from "../types";
import { Modal } from "./ui";
import { useToast } from "./toast";

type ListKey = "repair" | "optional" | "replace";
const LISTS: { key: ListKey; label: string; hint: string }[] = [
  { key: "repair", label: "Reparatur empfohlen", hint: "Struktur- und Zeitstempelfehler ohne Datenverlust – per Remux behebbar." },
  { key: "optional", label: "Kleinere Auffälligkeiten", hint: "Meist problemlos abspielbar – Remux optional, wenn der Player hakt." },
  { key: "replace", label: "Nicht mehr reparierbar", hint: "Notreparatur: zerstörte Bereiche werden übersprungen, die Datei ist danach wieder abspielbar und spulbar – dort fehlen aber weiterhin Bild und Ton. Besser: neu beschaffen." },
];

interface Candidates { items: RepairCandidate[]; output_mode: string; output_free: number | null }

/** Reparatur als Optimizer-Auftrag: Remux aller Spuren mit mkvmerge, Ergebnis wird vor dem Abschluss geprüft.
 *  Mit `category` wird vorher die Liste der offenen Dateien zum Auswählen angezeigt. */
export function RepairDialog({ title, fileIds, category, salvageDefault = false, showSalvage = true, onClose }: {
  title: string;
  fileIds?: number[];
  category?: ListKey;
  salvageDefault?: boolean;
  showSalvage?: boolean;
  onClose: () => void;
}) {
  const status = useOptStatus();
  const [salvageOpt, setSalvage] = useState(salvageDefault);
  const [list, setList] = useState<ListKey>(category ?? "repair");
  const salvage = category ? list === "replace" : salvageOpt;
  const [ok, setOk] = useState(false);
  const [strip, setStrip] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [topN, setTopN] = useState(5);
  const cands = useQuery({
    queryKey: ["repair-candidates"],
    queryFn: () => api<Candidates>("/api/optimizer/repair-candidates"),
    enabled: !!category,
  });
  const inList = (cands.data?.items ?? []).filter((c) => c.category === list);
  const open = inList.filter((c) => !c.repaired);
  const done = inList.filter((c) => c.repaired);
  const counts = (k: ListKey) => (cands.data?.items ?? []).filter((c) => c.category === k && !c.repaired).length;
  const pickedBytes = open.filter((c) => picked.has(c.id)).reduce((a, c) => a + c.size, 0);
  const free = cands.data?.output_free ?? null;
  const ids = category ? [...picked] : fileIds ?? [];
  const toast = useToast();
  const nav = useNavigate();
  const qc = useQueryClient();
  const mode = status.data?.ops.output_mode;
  const modeInfo = mode ? status.data?.modes[mode] : undefined;
  const replaces = mode === "replace_backup" || mode === "replace_delete";
  const m = useMutation({
    mutationFn: () => post<{ id: number; total_files: number }>("/api/optimizer/jobs", {
      kind: "repair", file_ids: ids, repair: { salvage, strip_tracks: strip },
    }),
    onSuccess: (j) => {
      toast(`Reparaturauftrag mit ${j.total_files} Datei(en) angelegt`, "ok");
      qc.invalidateQueries({ queryKey: ["opt-jobs"] });
      qc.invalidateQueries({ queryKey: ["repair-candidates"] });
      onClose();
      nav("/optimizer/auftraege");
    },
    onError: (e: Error) => toast(e.message, "error"),
  });
  return (
    <Modal open title={title} onClose={onClose} wide={!!category}
      footer={<>
        <button className="btn-secondary" onClick={onClose}>Abbrechen</button>
        <button className="btn-primary" disabled={!ok || m.isPending || !modeInfo?.available || ids.length === 0} onClick={() => m.mutate()}>
          <Wrench className="h-4 w-4" />{m.isPending ? "Auftrag wird angelegt …" : "Reparatur starten"}
        </button>
      </>}>
      <div className="space-y-3 text-sm">
        {category && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-1">
              {LISTS.map((l) => (
                <button key={l.key} className={list === l.key ? "btn-primary text-xs" : "btn-secondary text-xs"}
                  onClick={() => { setList(l.key); setPicked(new Set()); }}>
                  {l.label}{cands.data ? ` (${fmtInt(counts(l.key))})` : ""}
                </button>
              ))}
            </div>
            <p className={`text-xs ${list === "replace" ? "text-orange-500" : "text-slate-500"}`}>{LISTS.find((l) => l.key === list)!.hint}</p>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-slate-500">Obere</span>
              <input type="number" min={1} className="input w-20" value={topN}
                onChange={(e) => setTopN(Math.max(1, Number(e.target.value) || 1))} />
              <button className="btn-secondary text-xs" onClick={() => setPicked(new Set(open.filter((c) => !c.failed).slice(0, topN).map((c) => c.id)))}>auswählen</button>
              <button className="btn-ghost text-xs" onClick={() => setPicked(new Set(open.filter((c) => !c.failed).map((c) => c.id)))}>Alle</button>
              <button className="btn-ghost text-xs" onClick={() => setPicked(new Set())}>Keine</button>
            </div>
            <div className="text-xs">
              <b>{fmtInt(picked.size)}</b> von {fmtInt(open.length)} ausgewählt
              {open.some((c) => c.failed) && <> · {fmtInt(open.filter((c) => c.failed).length)} fehlgeschlagen (nur einzeln wählbar)</>} · {fmtBytes(pickedBytes)}
              {free !== null && <> · frei im Ziel: {fmtBytes(free)}</>}
              {free !== null && pickedBytes * 1.05 > free &&
                <span className="ml-1 text-red-500">– reicht voraussichtlich nicht für alle ausgewählten Filme</span>}
            </div>
            <div className="max-h-72 overflow-y-auto rounded border border-slate-500/30">
              {cands.isLoading && <div className="p-3 text-xs text-slate-500">Liste wird geladen …</div>}
              {cands.isError && (
                <div className="p-3 text-xs text-red-500">
                  Liste konnte nicht geladen werden: {(cands.error as Error).message}
                  <button className="btn-ghost ml-2 text-xs" onClick={() => cands.refetch()}>Erneut versuchen</button>
                </div>
              )}
              {cands.data && open.length === 0 && <div className="p-3 text-xs text-slate-500">Keine offenen Dateien in dieser Liste.</div>}
              {open.map((c) => (
                <label key={c.id} className="flex cursor-pointer items-start gap-2 border-b border-slate-500/20 px-2 py-1.5 last:border-0">
                  <input type="checkbox" className="mt-1" checked={picked.has(c.id)} onChange={(e) => {
                    const n = new Set(picked);
                    if (e.target.checked) n.add(c.id); else n.delete(c.id);
                    setPicked(n);
                  }} />
                  <span className="min-w-0 flex-1">
                    <span className="block break-words">{c.filename}</span>
                    <span className="block text-xs text-slate-500">{fmtBytes(c.size)}{c.reasons ? ` · ${c.reasons}` : ""}</span>
                    {c.failed && <span className="block text-xs text-red-500">Letzte Reparatur fehlgeschlagen: {c.failed}</span>}
                  </span>
                </label>
              ))}
            </div>
            {done.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer text-green-600 dark:text-green-400">Bereits repariert ({fmtInt(done.length)}) – nicht erneut auswählbar</summary>
                <ul className="mt-1 space-y-1">
                  {done.map((c) => <li key={c.id} className="break-words">{c.filename} <span className="text-slate-500">→ {c.repaired!.output_path}</span></li>)}
                </ul>
                <p className="mt-1 text-slate-500">Ersetzt du das Original durch die reparierte Fassung, wird der Film neu geprüft und verschwindet aus der Liste.</p>
              </details>
            )}
          </div>
        )}
        <p>Die Datei wird mit <b>mkvmerge</b> neu verpackt (alle Spuren bleiben erhalten). Dabei werden Index (Cues),
          Zeitstempel, Laufzeitangabe und Clusterstruktur neu aufgebaut und Restdaten nach dem Dateiende verworfen.
          Die neue Datei wird vor dem Abschluss auf Spuren und Struktur geprüft – erst dann wird sie übernommen.</p>
        <div className="rounded border border-slate-500/30 p-2 text-xs">
          <b>Ziel:</b> {modeInfo ? modeInfo.label : "…"}
          {mode === "output_dir" && <> (<code>{status.data?.output_root}</code>, Ordnerstruktur wie in der Bibliothek)</>}
          {modeInfo && !modeInfo.available && <div className="mt-1 text-red-500">{modeInfo.reason}</div>}
          {replaces && <div className="mt-1 text-orange-500">Das Original wird ersetzt{mode === "replace_backup" ? " (mit Sicherungskopie)" : " und gelöscht"}.</div>}
          <div className="mt-1 text-slate-500">Ändern unter <Link className="underline" to="/optimizer/einstellungen" onClick={onClose}>MKV-Optimizer → Einstellungen</Link>.</div>
        </div>
        {showSalvage && (
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={salvage} onChange={(e) => setSalvage(e.target.checked)} />
            <span><b>Notreparatur</b> auch für „nicht mehr reparierbare“ Dateien: mkvmerge überspringt zerstörte Bereiche,
              die Datei ist danach wieder abspielbar und spulbar – an diesen Stellen fehlen aber weiterhin Bild und Ton.</span>
          </label>
        )}
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-1" checked={strip} onChange={(e) => setStrip(e.target.checked)} />
          <span><b>Gleichzeitig überflüssige Spuren entfernen</b> – im selben Durchgang, nach den zuletzt im
            MKV-Optimizer verwendeten Regeln. Geschützte Sprachen bleiben immer erhalten, die letzte Audiospur wird nie entfernt.
            {strip && status.data && (
              <span className="mt-1 block text-xs">
                Behalten – Audio: {ruleText(status.data.last_rules.audio)} · Untertitel: {ruleText(status.data.last_rules.subtitles)}
                {" "}(<Link className="underline" to="/optimizer" onClick={onClose}>Regeln ändern</Link>)
              </span>
            )}
          </span>
        </label>
        <p className="text-xs text-slate-500">Dateien ohne Reparaturbedarf werden übersprungen. Nur MKV-Dateien.</p>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-1" checked={ok} onChange={(e) => setOk(e.target.checked)} />
          <span>Verstanden – Reparatur ausdrücklich starten.</span>
        </label>
      </div>
    </Modal>
  );
}

function ruleText(r: TrackRule): string {
  if (r.mode === "track_id") return r.track_ids.length ? `Spur-IDs ${r.track_ids.join(", ")}` : "keine";
  if (!r.languages.length) return "nur geschützte Sprachen";
  return r.languages.join(", ") + (r.codecs.length ? ` (bei mehreren Spuren je Sprache: ${r.codecs.join(", ")})` : "");
}
