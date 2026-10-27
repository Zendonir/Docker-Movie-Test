import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Wrench } from "lucide-react";
import { post } from "../api";
import { useOptStatus } from "../pages/optimizer/common";
import type { TrackRule } from "../types";
import { Modal } from "./ui";
import { useToast } from "./toast";

/** Reparatur als Optimizer-Auftrag: Remux aller Spuren mit mkvmerge, Ergebnis wird vor dem Abschluss geprüft. */
export function RepairDialog({ title, fileIds, category, salvageDefault = false, showSalvage = true, onClose }: {
  title: string;
  fileIds?: number[];
  category?: "repair" | "replace" | "repair_and_replace";
  salvageDefault?: boolean;
  showSalvage?: boolean;
  onClose: () => void;
}) {
  const status = useOptStatus();
  const [salvage, setSalvage] = useState(salvageDefault);
  const [ok, setOk] = useState(false);
  const [strip, setStrip] = useState(false);
  const toast = useToast();
  const nav = useNavigate();
  const qc = useQueryClient();
  const mode = status.data?.ops.output_mode;
  const modeInfo = mode ? status.data?.modes[mode] : undefined;
  const replaces = mode === "replace_backup" || mode === "replace_delete";
  const m = useMutation({
    mutationFn: () => post<{ id: number; total_files: number }>("/api/optimizer/jobs", {
      kind: "repair", file_ids: fileIds ?? [], category, repair: { salvage, strip_tracks: strip },
    }),
    onSuccess: (j) => {
      toast(`Reparaturauftrag mit ${j.total_files} Datei(en) angelegt`, "ok");
      qc.invalidateQueries({ queryKey: ["opt-jobs"] });
      onClose();
      nav("/optimizer/auftraege");
    },
    onError: (e: Error) => toast(e.message, "error"),
  });
  return (
    <Modal open title={title} onClose={onClose}
      footer={<>
        <button className="btn-secondary" onClick={onClose}>Abbrechen</button>
        <button className="btn-primary" disabled={!ok || m.isPending || !modeInfo?.available} onClick={() => m.mutate()}>
          <Wrench className="h-4 w-4" />Reparatur starten
        </button>
      </>}>
      <div className="space-y-3 text-sm">
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
