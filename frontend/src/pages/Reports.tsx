import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, Wrench } from "lucide-react";
import { RepairDialog } from "../components/RepairDialog";
import { api, qs } from "../api";
import type { Job, Library, Paged } from "../types";
import { Card } from "../components/ui";

const SCOPES = [
  { value: "replace", label: "Nicht mehr reparierbar", hint: "Datenverlust (zerstörte Cluster, fehlende Spielzeit, Bildfehler) – diese Dateien neu beschaffen bzw. neu rippen." },
  { value: "repair", label: "Sollten repariert werden", hint: "Struktur- und Zeitstempelfehler ohne Datenverlust – per Remux mit mkvmerge behebbar." },
  { value: "assessment", label: "Reparaturübersicht", hint: "Alle Dateien nach Empfehlung gruppiert: ersetzen, reparieren, kleinere Auffälligkeiten, nicht geprüft, in Ordnung." },
  { value: "library", label: "Gesamte Bibliothek", hint: "Alle Dateien mit Status (eine Zeile pro Datei)." },
  { value: "damaged", label: "Nur beschädigte Dateien", hint: "Kritisch und Fehler – inklusive Fehlerdetails. Grundlage für „Was muss ersetzt werden?“." },
  { value: "warnings", label: "Nur Warnungen", hint: "Auffällige Dateien ohne sicheren Defektnachweis." },
  { value: "job", label: "Einzelner Scanlauf", hint: "Alle Dateien und Ergebnisse eines Auftrags." },
];

export function Reports() {
  const [scope, setScope] = useState("replace");
  const [jobId, setJobId] = useState<number | "">("");
  const [libId, setLibId] = useState<number | "">("");
  const [details, setDetails] = useState<"auto" | "yes" | "no">("auto");
  const [delim, setDelim] = useState(";");
  const [repair, setRepair] = useState<null | "repair" | "replace">(null);
  const jobs = useQuery({ queryKey: ["jobs", "all-for-report"], queryFn: () => api<Paged<Job>>("/api/jobs?page_size=200") });
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/api/libraries") });
  const url = (format: string, inline = false) => `/api/export${qs({
    format, scope, job_id: scope === "job" ? jobId || undefined : undefined, library_id: scope !== "job" ? libId || undefined : undefined,
    details: details === "auto" ? undefined : details === "yes", delimiter: format === "csv" ? delim : undefined, inline: inline || undefined,
  })}`;
  const disabled = scope === "job" && !jobId;
  return (
    <div className="space-y-4">
      <div><h1 className="text-xl font-semibold">Berichte und Export</h1><p className="text-sm text-slate-500">CSV, JSON und HTML – UTF-8 mit korrekten Umlauten und Sonderzeichen. Einzeldateien lassen sich auf der Detailseite exportieren.</p></div>
      <Card title="Bericht erstellen">
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-2">
            <label className="label">Umfang</label>
            {SCOPES.map((s) => (
              <label key={s.value} className="flex cursor-pointer items-start gap-2 text-sm">
                <input type="radio" className="mt-1" checked={scope === s.value} onChange={() => setScope(s.value)} />
                <span><span className="font-medium">{s.label}</span><span className="block text-xs text-slate-500">{s.hint}</span></span>
              </label>
            ))}
          </div>
          <div className="space-y-3">
            {scope === "job" ? (
              <div><label className="label">Scanlauf</label>
                <select className="input" value={jobId} onChange={(e) => setJobId(e.target.value ? Number(e.target.value) : "")}>
                  <option value="">Bitte wählen …</option>
                  {jobs.data?.items.map((j) => <option key={j.id} value={j.id}>#{j.id} – {j.name} ({new Date(j.created_at).toLocaleString("de-DE")})</option>)}
                </select>
              </div>
            ) : (
              <div><label className="label">Bibliothek</label>
                <select className="input" value={libId} onChange={(e) => setLibId(e.target.value ? Number(e.target.value) : "")}>
                  <option value="">Alle Bibliotheken</option>
                  {libs.data?.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
                </select>
              </div>
            )}
            <div><label className="label">Fehlerdetails</label>
              <select className="input" value={details} onChange={(e) => setDetails(e.target.value as typeof details)}>
                <option value="auto">Automatisch (bei beschädigten Dateien und Scanläufen; Reparaturlisten ohne)</option>
                <option value="yes">Immer – eine Zeile pro Befund</option>
                <option value="no">Nie – eine Zeile pro Datei</option>
              </select>
            </div>
            <div><label className="label">CSV-Trennzeichen</label>
              <select className="input w-auto" value={delim} onChange={(e) => setDelim(e.target.value)}>
                <option value=";">Semikolon (Excel, deutsch)</option>
                <option value=",">Komma</option>
                <option value={"\t"}>Tabulator</option>
              </select>
            </div>
            <div className="flex flex-wrap gap-2 pt-2">
              <a className={`btn-primary ${disabled ? "pointer-events-none opacity-50" : ""}`} href={url("csv")}><Download className="h-4 w-4" />CSV</a>
              <a className={`btn-secondary ${disabled ? "pointer-events-none opacity-50" : ""}`} href={url("json")}><Download className="h-4 w-4" />JSON</a>
              <a className={`btn-secondary ${disabled ? "pointer-events-none opacity-50" : ""}`} href={url("html")}><Download className="h-4 w-4" />HTML</a>
              <a className={`btn-ghost ${disabled ? "pointer-events-none opacity-50" : ""}`} href={url("html", true)} target="_blank" rel="noreferrer"><ExternalLink className="h-4 w-4" />HTML im Browser öffnen</a>
            </div>
          </div>
        </div>
      </Card>
      <Card title="Direkt reparieren">
        <div className="space-y-3 text-sm">
          <p>Zeigt die MKV-Dateien der jeweiligen Liste zum Auswählen an – z. B. die oberen 5, damit der Platz im Ziel
            reicht – und legt dafür einen Reparaturauftrag an (Remux mit mkvmerge, Prüfung der neuen Datei vor der Übernahme).
            Bereits reparierte Filme werden als „Repariert“ geführt und nicht erneut angeboten. Fortschritt und Ergebnis unter <b>MKV-Optimizer → Aufträge</b>.</p>
          <div className="flex flex-wrap gap-2">
            <button className="btn-primary" onClick={() => setRepair("repair")}><Wrench className="h-4 w-4" />„Sollten repariert werden“ reparieren …</button>
            <button className="btn-secondary" onClick={() => setRepair("replace")}><Wrench className="h-4 w-4" />Notreparatur für „Nicht mehr reparierbar“</button>
          </div>
        </div>
      </Card>
      {repair && <RepairDialog title={repair === "repair" ? "Filme zum Reparieren auswählen" : "Notreparatur nicht reparierbarer Dateien"}
        category={repair} salvageDefault={repair === "replace"} showSalvage={false} onClose={() => setRepair(null)} />}
    </div>
  );
}
