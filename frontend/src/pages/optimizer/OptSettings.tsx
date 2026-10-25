import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Pencil, Plus, Save, Trash2 } from "lucide-react";
import { api, del, post, put } from "../../api";
import type { OptProfile, Settings } from "../../types";
import { Card, ErrorBox, Loading, Modal, Toggle } from "../../components/ui";
import { useToast } from "../../components/toast";
import { TagInput, useOptStatus } from "./common";

type ProfileForm = Omit<OptProfile, "id" | "builtin" | "codec_label" | "encoder_available">;
const NEW_PROFILE: ProfileForm = { name: "", codec: "ac3", bitrate_kbps: 640, max_channels: 6, sample_rate: null, source_codecs: ["DTS", "DTS-HD", "TRUEHD"], source_languages: [], mode: "replace", track_name: "AC3 {channels}", filename_pattern: "{name}" };
const AUDIO_FAMILIES = ["AC3", "EAC3", "DTS", "DTS-HD", "TRUEHD", "AAC", "FLAC", "PCM", "OPUS", "MP3", "VORBIS"].map((v) => ({ value: v, label: v, count: 0 }));

function ProfileEditor({ initial, id, onClose }: { initial: ProfileForm; id?: number; onClose: () => void }) {
  const [f, setF] = useState<ProfileForm>(initial);
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => (id ? put(`/api/optimizer/profiles/${id}`, f) : post("/api/optimizer/profiles", f)),
    onSuccess: () => { toast("Profil gespeichert", "ok"); qc.invalidateQueries({ queryKey: ["opt-profiles"] }); onClose(); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const set = <K extends keyof ProfileForm>(k: K, v: ProfileForm[K]) => setF({ ...f, [k]: v });
  const num = (v: string) => (v === "" ? null : Number(v));
  return (
    <Modal open wide title={id ? "Profil bearbeiten" : "Neues Profil"} onClose={onClose}
      footer={<><button className="btn-secondary" onClick={onClose}>Abbrechen</button><button className="btn-primary" disabled={!f.name || save.isPending} onClick={() => save.mutate()}>Speichern</button></>}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <div><label className="label">Name</label><input className="input" value={f.name} onChange={(e) => set("name", e.target.value)} /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><label className="label">Zielcodec</label>
              <select className="input" value={f.codec} onChange={(e) => set("codec", e.target.value)}>
                {[["ac3", "AC3 (Dolby Digital)"], ["eac3", "E-AC3 (DD+)"], ["aac", "AAC"], ["flac", "FLAC"], ["opus", "Opus"], ["mp3", "MP3"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select></div>
            <div><label className="label">Bitrate (kbit/s)</label><input type="number" className="input" value={f.bitrate_kbps ?? ""} onChange={(e) => set("bitrate_kbps", num(e.target.value))} disabled={f.codec === "flac"} /></div>
            <div><label className="label">Max. Kanäle</label>
              <select className="input" value={f.max_channels ?? ""} onChange={(e) => set("max_channels", num(e.target.value))}>
                <option value="">wie Quelle</option><option value="2">2.0 (Stereo-Downmix)</option><option value="6">5.1</option><option value="8">7.1</option>
              </select></div>
            <div><label className="label">Abtastrate</label>
              <select className="input" value={f.sample_rate ?? ""} onChange={(e) => set("sample_rate", num(e.target.value))}>
                <option value="">wie Quelle (AC3: 48 kHz)</option><option value="48000">48 kHz</option><option value="44100">44,1 kHz</option>
              </select></div>
          </div>
          <div><label className="label">Modus</label>
            <select className="input" value={f.mode} onChange={(e) => set("mode", e.target.value as ProfileForm["mode"])}>
              <option value="replace">Originalspur ersetzen</option><option value="add">Konvertierte Spur zusätzlich einfügen</option>
            </select></div>
        </div>
        <div className="space-y-3">
          <div><label className="label">Quellspuren mit Codec (leer = alle)</label><TagInput values={f.source_codecs} onChange={(v) => set("source_codecs", v)} suggestions={AUDIO_FAMILIES} upper /></div>
          <div><label className="label">Nur Sprachen (leer = alle)</label><TagInput values={f.source_languages} onChange={(v) => set("source_languages", v)} placeholder="z. B. ger" /></div>
          <div><label className="label">Spurname</label><input className="input" value={f.track_name ?? ""} onChange={(e) => set("track_name", e.target.value || null)} /><p className="mt-1 text-xs text-slate-500">Platzhalter: {"{codec}"}, {"{channels}"}, {"{lang}"}</p></div>
          <div><label className="label">Dateiname der neuen Datei</label><input className="input font-mono" value={f.filename_pattern} onChange={(e) => set("filename_pattern", e.target.value)} /><p className="mt-1 text-xs text-slate-500">Platzhalter: {"{name}"} (Originalname), {"{codec}"}, {"{profile}"}; „.mkv“ wird angehängt. Gilt für die Modi „Ausgabeverzeichnis“ und „neben dem Original“.</p></div>
        </div>
      </div>
    </Modal>
  );
}

export function OptSettingsTab() {
  const status = useOptStatus();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("/api/settings") });
  const profiles = useQuery({ queryKey: ["opt-profiles"], queryFn: () => api<OptProfile[]>("/api/optimizer/profiles") });
  const [s, setS] = useState<Settings | null>(null);
  const [edit, setEdit] = useState<{ form: ProfileForm; id?: number } | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  useEffect(() => { if (settings.data) setS(settings.data); }, [settings.data]);
  const keys = ["optimizer_output_mode", "optimizer_allow_modify_originals", "optimizer_suffix", "optimizer_backup_dir", "optimizer_remove_metadata", "optimizer_reorder_tracks", "optimizer_set_default_track", "optimizer_protected_languages", "optimizer_verify_output", "optimizer_skip_damaged"];
  const save = useMutation({
    mutationFn: () => put<Settings>("/api/settings", Object.fromEntries(keys.map((k) => [k, s![k]]))),
    onSuccess: (d) => { toast("Gespeichert", "ok"); qc.setQueryData(["settings"], d); qc.invalidateQueries({ queryKey: ["opt-status"] }); qc.invalidateQueries({ queryKey: ["opt-preview"] }); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const remove = useMutation({ mutationFn: (id: number) => del(`/api/optimizer/profiles/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["opt-profiles"] }), onError: (e: Error) => toast(e.message, "error") });
  if (!s || !status.data) return <Loading />;
  const set = (k: string, v: unknown) => setS({ ...s, [k]: v } as Settings);
  const mode = s.optimizer_output_mode as string;
  const modes = status.data.modes;
  const toForm = (p: OptProfile): ProfileForm => ({ name: p.name, codec: p.codec, bitrate_kbps: p.bitrate_kbps, max_channels: p.max_channels, sample_rate: p.sample_rate, source_codecs: p.source_codecs, source_languages: p.source_languages, mode: p.mode, track_name: p.track_name, filename_pattern: p.filename_pattern });
  return (
    <div className="space-y-4">
      <Card title="Dateioperationen" actions={<button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}><Save className="h-4 w-4" />Speichern</button>}>
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-2">
            <label className="label">Was passiert mit konvertierten und Originaldateien?</label>
            {Object.entries(modes).map(([k, m]) => (
              <label key={k} className={`flex items-start gap-2 rounded-lg border p-2 text-sm ${mode === k ? "border-sky-500 bg-sky-500/5" : "border-slate-200 dark:border-slate-700"} ${m.available ? "cursor-pointer" : "opacity-60"}`}>
                <input type="radio" className="mt-1" checked={mode === k} onChange={() => set("optimizer_output_mode", k)} />
                <span><span className="font-medium">{m.label}</span>
                  {k === "output_dir" && <span className="block text-xs text-slate-500">Ziel: {status.data.output_root}/… (gleiche Ordnerstruktur wie unter /media)</span>}
                  {!m.available && <span className="block text-xs text-amber-500">{m.reason}</span>}</span>
              </label>
            ))}
            {mode.startsWith("replace") && (
              <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3">
                <Toggle checked={!!s.optimizer_allow_modify_originals} onChange={(v) => set("optimizer_allow_modify_originals", v)}
                  label="Veränderung von Originaldateien erlauben"
                  hint="Erforderlich für „Original ersetzen“. Das Medienverzeichnis muss dafür beschreibbar (ohne :ro) eingebunden sein. Jede neue Datei wird vor dem Ersetzen geprüft." />
              </div>
            )}
            <div className="grid grid-cols-2 gap-3 pt-2">
              <div><label className="label">Suffix (neben dem Original)</label><input className="input font-mono" value={String(s.optimizer_suffix ?? "")} onChange={(e) => set("optimizer_suffix", e.target.value)} /></div>
              <div><label className="label">Sicherungsordner (relativ zur Bibliothek)</label><input className="input font-mono" value={String(s.optimizer_backup_dir ?? "")} onChange={(e) => set("optimizer_backup_dir", e.target.value)} /></div>
            </div>
          </div>
          <div className="space-y-3">
            <Toggle checked={!!s.optimizer_remove_metadata} onChange={(v) => set("optimizer_remove_metadata", v)} label="Metadaten entfernen" hint="Globale Tags, Spur-Tags und Titel werden nicht übernommen (Spurnamen bleiben)." />
            <Toggle checked={!!s.optimizer_reorder_tracks} onChange={(v) => set("optimizer_reorder_tracks", v)} label="Spuren neu sortieren" hint="Video, dann Audio und Untertitel in der Reihenfolge der gewählten Sprachen." />
            <Toggle checked={!!s.optimizer_set_default_track} onChange={(v) => set("optimizer_set_default_track", v)} label="Standard-Audiospur setzen" hint="Wird die bisherige Standardspur entfernt, wird die erste verbleibende Spur Standard." />
            <Toggle checked={!!s.optimizer_verify_output} onChange={(v) => set("optimizer_verify_output", v)} label="Neue Datei vor dem Abschluss strukturell prüfen" hint="EBML-Strukturprüfung zusätzlich zur Spur- und Laufzeitprüfung (empfohlen)." />
            <Toggle checked={!!s.optimizer_skip_damaged} onChange={(v) => set("optimizer_skip_damaged", v)} label="Dateien mit Integritätsfehlern überspringen" />
            <div><label className="label">Sprachen, die nie vollständig entfernt werden</label>
              <TagInput values={(s.optimizer_protected_languages as string[]) ?? []} onChange={(v) => set("optimizer_protected_languages", v)} placeholder="z. B. ger" /></div>
          </div>
        </div>
      </Card>

      <Card title="Profile für die Audiokonvertierung" actions={<button className="btn-primary" onClick={() => setEdit({ form: NEW_PROFILE })}><Plus className="h-4 w-4" />Neues Profil</button>}>
        <ErrorBox error={profiles.error} />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead><tr><th className="th">Name</th><th className="th">Ziel</th><th className="th">Quellen</th><th className="th">Modus</th><th className="th">Dateiname</th><th className="th" /></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {profiles.data?.map((p) => (
                <tr key={p.id}>
                  <td className="td font-medium">{p.name}{p.builtin && <span className="ml-1 text-[11px] text-slate-500">(vordefiniert)</span>}{!p.encoder_available && <div className="text-[11px] text-red-500">Encoder in FFmpeg nicht verfügbar</div>}</td>
                  <td className="td text-xs">{p.codec_label}{p.bitrate_kbps ? `, ${p.bitrate_kbps} kbit/s` : ""}{p.max_channels ? `, max. ${p.max_channels} Kan.` : ""}</td>
                  <td className="td text-xs">{p.source_codecs.join(", ") || "alle"}{p.source_languages.length ? ` · ${p.source_languages.join(", ").toUpperCase()}` : ""}</td>
                  <td className="td text-xs">{p.mode === "replace" ? "ersetzen" : "zusätzlich"}</td>
                  <td className="td font-mono text-xs">{p.filename_pattern}.mkv</td>
                  <td className="td whitespace-nowrap text-right">
                    <button className="btn-ghost p-1.5" title="Kopieren" onClick={() => setEdit({ form: { ...toForm(p), name: `${p.name} (Kopie)` } })}><Copy className="h-4 w-4" /></button>
                    {!p.builtin && <button className="btn-ghost p-1.5" title="Bearbeiten" onClick={() => setEdit({ form: toForm(p), id: p.id })}><Pencil className="h-4 w-4" /></button>}
                    {!p.builtin && <button className="btn-ghost p-1.5 text-red-500" title="Löschen" onClick={() => confirm(`Profil „${p.name}“ löschen?`) && remove.mutate(p.id)}><Trash2 className="h-4 w-4" /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Werkzeuge">
        <p className="text-sm">mkvmerge {status.data.tools.mkvmerge ?? <span className="text-red-500">fehlt</span>} · FFmpeg {status.data.tools.ffmpeg ?? <span className="text-red-500">fehlt</span>} · Encoder: {Object.entries(status.data.encoders).map(([k, v]) => `${k.toUpperCase()} ${v ? "✓" : "✗"}`).join(", ")}</p>
        <p className="mt-1 text-xs text-slate-500">MKVToolNix und FFmpeg sind im Docker-Image enthalten – eine Pfadangabe ist nicht nötig.</p>
      </Card>
      {edit && <ProfileEditor initial={edit.form} id={edit.id} onClose={() => setEdit(null)} />}
    </div>
  );
}
