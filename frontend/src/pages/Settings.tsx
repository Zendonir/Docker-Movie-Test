import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { api, post, put } from "../api";
import type { Method, Settings } from "../types";
import { Card, ErrorBox, Loading, Toggle } from "../components/ui";
import { MethodPicker } from "./Files";
import { useToast } from "../components/toast";

interface SystemInfo { version: string; tools: Record<string, { path?: string; version?: string; available?: boolean } | unknown>; scanner_versions: Record<string, number>; media_roots: string[]; config_dir: string; uid: number; gid: number; tz: string; repair_enabled: boolean; media_writable: string[] }

function Num({ label, value, onChange, min, max, step, hint }: { label: string; value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; hint?: string }) {
  return (
    <div>
      <label className="label">{label}</label>
      <input type="number" className="input w-32" value={value} min={min} max={max} step={step ?? 1} onChange={(e) => onChange(Number(e.target.value))} />
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function SettingsPage() {
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("/api/settings") });
  const sys = useQuery({ queryKey: ["system"], queryFn: () => api<SystemInfo>("/api/system") });
  const [s, setS] = useState<Settings | null>(null);
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const qc = useQueryClient();
  const toast = useToast();
  useEffect(() => { if (q.data) setS(q.data); }, [q.data]);
  const save = useMutation({
    mutationFn: () => {
      const { auth_enabled, auth_username, auth_password_set, ...rest } = s!;
      void auth_enabled; void auth_username; void auth_password_set;
      return put<Settings>("/api/settings", rest);
    },
    onSuccess: (d) => { toast("Einstellungen gespeichert", "ok"); qc.setQueryData(["settings"], d); qc.invalidateQueries({ queryKey: ["stats"] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const setAuth = useMutation({
    mutationFn: () => post("/api/settings/auth", { username: user, password: pw, enable: true }),
    onSuccess: () => { toast("Anmeldung aktiviert", "ok"); qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["auth"] }); setPw(""); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const disableAuth = useMutation({
    mutationFn: () => post("/api/settings/auth/disable"),
    onSuccess: () => { toast("Anmeldung deaktiviert", "ok"); qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["auth"] }); },
  });
  if (q.isLoading || !s) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setS({ ...s, [k]: v });
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div><h1 className="text-xl font-semibold">Einstellungen</h1><p className="text-sm text-slate-500">Werte werden in /config gespeichert und gelten sofort (laufende Prüfungen behalten ihre Startwerte).</p></div>
        <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}><Save className="h-4 w-4" />Speichern</button>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Ressourcen (NAS-schonend)">
          <div className="grid gap-4 sm:grid-cols-2">
            <Num label="Parallele Prüfungen" value={s.max_concurrent_scans} min={1} max={32} onChange={(v) => set("max_concurrent_scans", v)} hint="Standard 1 – jede Vollprüfung belastet CPU und Festplatten." />
            <Num label="FFmpeg-Threads je Prüfung" value={s.ffmpeg_threads} min={0} max={64} onChange={(v) => set("ffmpeg_threads", v)} hint="0 = automatisch (alle Kerne)." />
            <Num label="CPU-Priorität (nice)" value={s.nice_level} min={0} max={19} onChange={(v) => set("nice_level", v)} hint="19 = niedrigste Priorität." />
            <div><label className="label">E/A-Priorität (ionice)</label>
              <select className="input" value={s.io_priority} onChange={(e) => set("io_priority", e.target.value)}>
                <option value="idle">Leerlauf (nur wenn sonst nichts liest)</option>
                <option value="best-effort-low">Niedrig (Standard)</option>
                <option value="best-effort">Mittel</option>
                <option value="normal">Normal</option>
              </select>
            </div>
            <Num label="Lesegeschwindigkeit (× Echtzeit)" value={s.read_rate_limit} min={0} max={1000} step={0.5} onChange={(v) => set("read_rate_limit", v)} hint="FFmpeg -readrate; 0 = unbegrenzt. Beispiel 8 = höchstens 8-fache Abspielgeschwindigkeit." />
            <div><label className="label">Hardwarebeschleunigung</label>
              <select className="input" value={s.hwaccel} onChange={(e) => set("hwaccel", e.target.value)}>
                {["none", "auto", "vaapi", "qsv", "cuda", "vdpau", "drm", "vulkan"].map((h) => <option key={h} value={h}>{h === "none" ? "Aus (Software – empfohlen)" : h}</option>)}
              </select>
              <p className="mt-1 text-xs text-slate-500">Hardwaredecoder melden Fehler teils anders; Softwaredecodierung ist zuverlässiger.</p>
            </div>
            <div><label className="label">Pausieren</label>
              <select className="input" value={s.pause_mode} onChange={(e) => set("pause_mode", e.target.value as Settings["pause_mode"])}>
                <option value="immediate">Sofort (Prozesse werden angehalten)</option>
                <option value="after_file">Nach Abschluss der aktuellen Datei</option>
              </select>
            </div>
            <Num label="Hänger-Erkennung (Sekunden)" value={s.stall_timeout_s} min={60} max={86400} onChange={(v) => set("stall_timeout_s", v)} hint="Werkzeug ohne Ausgabe wird danach beendet." />
          </div>
        </Card>

        <Card title="Prüfverfahren">
          <div className="space-y-4">
            <div><label className="label">Standard-Methoden für neue Scans</label>
              <MethodPicker methods={s.default_methods as Method[]} toggle={(m) => set("default_methods", (s.default_methods.includes(m) ? s.default_methods.filter((x) => x !== m) : [...s.default_methods, m]) as Method[])} />
            </div>
            <div className="space-y-2">
              <Toggle checked={s.container_ebml} onChange={(v) => set("container_ebml", v)} label="EBML-Strukturprüfung (MKV)" hint="Eigener Parser: liest jedes Byte, prüft Cluster, Blöcke, CRC, Zeitstempel, Index." />
              <Toggle checked={s.container_mkvmerge} onChange={(v) => set("container_mkvmerge", v)} label="mkvmerge-Volldurchlauf (MKV)" hint="mkvmerge -o /dev/null: liest alle Blöcke über MKVToolNix." />
              <Toggle checked={s.container_ffmpeg_demux} onChange={(v) => set("container_ffmpeg_demux", v)} label="FFmpeg-Demux-Durchlauf" hint="Bei gleichzeitiger Vollprüfung automatisch übersprungen (die Decodierung demuxt bereits)." />
              <Toggle checked={s.container_remux_check} onChange={(v) => set("container_remux_check", v)} label="Diagnose-Remux in temporäre Datei" hint="Benötigt freien Speicher in /config/tmp in Dateigröße. Original wird nie verändert." />
              <Toggle checked={s.strict_decode} onChange={(v) => set("strict_decode", v)} label="Strenge Decoderprüfung" hint="-err_detect crccheck+bitstream+buffer – meldet mehr, auch Grenzfälle." />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Num label="Zeitsprung Warnung ab (s)" value={s.ts_jump_warning_s} step={0.5} onChange={(v) => set("ts_jump_warning_s", v)} />
              <Num label="Zeitsprung Fehler ab (s)" value={s.ts_jump_error_s} onChange={(v) => set("ts_jump_error_s", v)} />
              <Num label="Spurlücke Warnung ab (s)" value={s.track_gap_warning_s} step={0.5} onChange={(v) => set("track_gap_warning_s", v)} />
              <Num label="Laufzeittoleranz (s)" value={s.duration_tolerance_s} onChange={(v) => set("duration_tolerance_s", v)} />
              <Num label="Laufzeittoleranz (%)" value={s.duration_tolerance_pct} step={0.5} onChange={(v) => set("duration_tolerance_pct", v)} />
              <Num label="Max. Befunde je Prüfung" value={s.max_findings_per_result} min={10} max={10000} onChange={(v) => set("max_findings_per_result", v)} />
            </div>
            <Toggle checked={s.keep_scan_logs} onChange={(v) => set("keep_scan_logs", v)} label="Vollständige FFmpeg-Protokolle speichern" hint="Komprimiert unter /config/logs/scans." />
          </div>
        </Card>

        <Card title="Bibliothek und Dateierkennung">
          <div className="space-y-4">
            <div><label className="label">Dateiendungen</label><input className="input font-mono" value={s.extensions.join(", ")} onChange={(e) => set("extensions", e.target.value.split(/[,\s]+/).filter(Boolean))} /></div>
            <div><label className="label">Globale Ausschlussmuster (je Zeile, fnmatch auf Name oder relativen Pfad)</label><textarea className="input h-32 font-mono text-xs" value={s.exclude_patterns.join("\n")} onChange={(e) => set("exclude_patterns", e.target.value.split("\n"))} /></div>
            <div><label className="label">Hashwerte</label>
              <select className="input" value={s.hash_mode} onChange={(e) => set("hash_mode", e.target.value as Settings["hash_mode"])}>
                <option value="none">Keine (nur Pfad, Größe, Änderungszeit)</option>
                <option value="quick">Schnell-Hash (3 × 64 KiB) – Standard</option>
                <option value="full">Schnell-Hash + vollständiger SHA-256 bei Container-/Vollprüfung (erkennt stille Datenänderungen, doppelte Lesezugriffe)</option>
              </select>
              <p className="mt-1 text-xs text-slate-500">Der Hash hilft, verschobene oder umbenannte Dateien sicher wiederzuerkennen.</p>
            </div>
            <Toggle checked={s.follow_symlinks} onChange={(v) => set("follow_symlinks", v)} label="Symlinks folgen" hint="Nur Ziele innerhalb derselben Bibliothek werden akzeptiert." />
            <Toggle checked={s.auto_resume_after_restart} onChange={(v) => set("auto_resume_after_restart", v)} label="Unterbrochene Scans nach Neustart automatisch fortsetzen" />
          </div>
        </Card>

        <Card title="Anmeldung">
          <div className="space-y-3">
            <p className="text-sm">{s.auth_enabled ? <>Anmeldung ist <b>aktiv</b> (Benutzer: {s.auth_username}).</> : "Anmeldung ist deaktiviert – jeder im Netzwerk kann die Oberfläche nutzen."}</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <div><label className="label">Benutzername</label><input className="input" value={user} onChange={(e) => setUser(e.target.value)} placeholder={s.auth_username ?? "admin"} /></div>
              <div><label className="label">Neues Passwort (min. 8 Zeichen)</label><input type="password" className="input" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" /></div>
            </div>
            <div className="flex gap-2">
              <button className="btn-primary" disabled={!user || pw.length < 8 || setAuth.isPending} onClick={() => setAuth.mutate()}>Anmeldung aktivieren / Passwort setzen</button>
              {s.auth_enabled && <button className="btn-secondary" onClick={() => confirm("Anmeldung wirklich deaktivieren?") && disableAuth.mutate()}>Deaktivieren</button>}
            </div>
            <p className="text-xs text-slate-500">Alternativ per Umgebungsvariablen AUTH_ENABLED, AUTH_USERNAME, AUTH_PASSWORD (haben Vorrang).</p>
          </div>
        </Card>

        <Card title="System" className="xl:col-span-2">
          {sys.data ? (
            <div className="grid gap-4 text-sm md:grid-cols-2">
              <dl className="space-y-1">
                <div>Version: <b>{sys.data.version}</b></div>
                <div>Medienverzeichnisse: <span className="font-mono">{sys.data.media_roots.join(", ")}</span></div>
                {sys.data.media_writable.length > 0 && <div className="text-amber-500">Achtung: beschreibbar eingebunden ({sys.data.media_writable.join(", ")}) – empfohlen ist schreibgeschützt (:ro).</div>}
                <div>Konfiguration: <span className="font-mono">{sys.data.config_dir}</span></div>
                <div>Läuft als UID/GID: {sys.data.uid}/{sys.data.gid}</div>
                <div>Zeitzone: {sys.data.tz}</div>
                <div>Reparaturfunktion: {sys.data.repair_enabled ? "aktiviert (REPAIR_ENABLED)" : "deaktiviert"}</div>
                <div>Prüferversionen: {Object.entries(sys.data.scanner_versions).map(([k, v]) => `${k} v${v}`).join(", ")}</div>
              </dl>
              <dl className="space-y-1">
                {Object.entries(sys.data.tools).filter(([, v]) => typeof v === "object" && v !== null && "available" in (v as object)).map(([k, v]) => {
                  const t = v as { version?: string; available?: boolean };
                  return <div key={k}>{k}: {t.available ? <b>{t.version ?? "vorhanden"}</b> : <span className="text-red-500">fehlt</span>}</div>;
                })}
                <div>Python: {String(sys.data.tools.python)}</div>
                <div>Decodierbare Codecs: {String(sys.data.tools.decodable_codecs)}</div>
              </dl>
            </div>
          ) : <Loading />}
        </Card>
      </div>
    </div>
  );
}
