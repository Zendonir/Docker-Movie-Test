import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { api, post } from "../../api";
import { fmtBytes, fmtInt } from "../../format";
import type { OptFile, OptPreview, OptProfile } from "../../types";
import { Empty, ErrorBox, Loading, Modal, Pagination, Card } from "../../components/ui";
import { useToast } from "../../components/toast";
import { CommandModal, ContextMenu, Folder, FolderPicker, OUTPUT_MODE_HINT, Saving, SearchButton, TrackChip, planBadge, sumPredictions, useOptStatus } from "./common";

export function AudioTab() {
  const status = useOptStatus();
  const toast = useToast();
  const nav = useNavigate();
  const profiles = useQuery({ queryKey: ["opt-profiles"], queryFn: () => api<OptProfile[]>("/api/optimizer/profiles") });
  const [folder, setFolder] = useState<Folder>({ library_id: null, directory: "", recursive: true });
  const [profileId, setProfileId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [page, setPage] = useState(1);
  const [onlyChanged, setOnlyChanged] = useState(true);
  const [detail, setDetail] = useState<OptFile | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [confirm, setConfirm] = useState(false);
  const pid = profileId ?? profiles.data?.[0]?.id ?? null;
  const profile = profiles.data?.find((x) => x.id === pid);
  const body = { library_id: folder.library_id, directory: folder.directory, recursive: folder.recursive, only_multi_audio: false, kind: "audio", profile_id: pid, only_changed: onlyChanged, page, page_size: 100 };
  const preview = useQuery({
    queryKey: ["opt-preview", "audio", body], queryFn: () => post<OptPreview>("/api/optimizer/preview", body),
    enabled: folder.library_id != null && pid != null, placeholderData: keepPreviousData,
  });
  const create = useMutation({
    mutationFn: () => post<{ id: number }>("/api/optimizer/jobs", { kind: "audio", file_ids: [...selected], profile_id: pid }),
    onSuccess: (j) => { toast(`Auftrag #${j.id} gestartet`, "ok"); setSelected(new Set()); setConfirm(false); nav("/optimizer/auftraege"); },
    onError: (e: Error) => { toast(e.message, "error"); setConfirm(false); },
  });
  const p = preview.data;
  const items = p?.items ?? [];
  const toggle = (id: number) => { const n = new Set(selected); if (n.has(id)) n.delete(id); else n.add(id); setSelected(n); };
  const selChanged = [...selected].filter((id) => p?.changed_ids.includes(id)).length;
  const mode = status.data?.ops.output_mode ?? "output_dir";
  const selPred = sumPredictions(selected, p?.predictions);
  if (profiles.isLoading || status.isLoading) return <Loading />;
  return (
    <div className="space-y-4">
      <Card title="Ordner und Profil">
        <div className="space-y-3">
          <FolderPicker value={folder} onChange={(f) => { setFolder(f); setSelected(new Set()); setPage(1); }}>
            <div>
              <label className="label">Profil</label>
              <select className="input w-auto max-w-[420px]" value={pid ?? ""} onChange={(e) => { setProfileId(Number(e.target.value)); setSelected(new Set()); }}>
                {profiles.data?.map((x) => <option key={x.id} value={x.id} disabled={!x.encoder_available}>{x.name}{x.builtin ? "" : " (eigenes)"}{x.encoder_available ? "" : " – Encoder fehlt"}</option>)}
              </select>
            </div>
          </FolderPicker>
          {profile && (
            <p className="text-xs text-slate-500">
              {profile.codec_label}{profile.bitrate_kbps ? `, ${profile.bitrate_kbps} kbit/s` : ""}{profile.max_channels ? `, max. ${profile.max_channels} Kanäle` : ""} ·
              Quellen: {profile.source_codecs.length ? profile.source_codecs.join(", ") : "alle Audiospuren"}{profile.source_languages.length ? ` (${profile.source_languages.join(", ").toUpperCase()})` : ""} ·
              {profile.mode === "replace" ? " Originalspur wird ersetzt" : " konvertierte Spur wird zusätzlich eingefügt"} · Dateiname: <code>{profile.filename_pattern}.mkv</code>
            </p>
          )}
          <SearchButton folder={folder} />
        </div>
      </Card>

      <div className="card overflow-hidden" onContextMenu={(e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); }}>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <div className="text-sm"><b>{fmtInt(p?.total ?? 0)}</b> Dateien · <span className="text-sky-500">{fmtInt(p?.changed_ids.length ?? 0)} mit passenden Spuren</span>{p && p.unanalyzed > 0 && <span className="text-amber-500"> · {fmtInt(p.unanalyzed)} nicht analysiert</span>}
            {p && p.changed_ids.length > 0 && <div className="mt-0.5 text-xs text-slate-500">Voraussichtliche Größenänderung: <Saving bytes={p.prediction.saved} compact /> bei allen passenden Dateien{selected.size > 0 && <> · markiert: <Saving bytes={selPred.bytes} compact /></>}</div>}</div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={onlyChanged} onChange={(e) => { setOnlyChanged(e.target.checked); setPage(1); }} />nur Dateien mit passenden Spuren</label>
            <button className="btn-secondary" onClick={() => setSelected(new Set(p?.changed_ids ?? []))}>Alle markieren</button>
            <button className="btn-ghost" onClick={() => setSelected(new Set())}>Keine</button>
            <button className="btn-primary" disabled={!selChanged} onClick={() => setConfirm(true)}><Play className="h-4 w-4" />Konvertieren ({fmtInt(selChanged)})</button>
          </div>
        </div>
        <ErrorBox error={preview.error} />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px]">
            <thead className="bg-slate-50 dark:bg-slate-900/60"><tr><th className="th w-8" /><th className="th">Datei</th><th className="th">Größe</th><th className="th">Audiospuren</th><th className="th">Größe danach</th><th className="th">Ergebnis</th></tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((f) => {
                const conv = new Set((f.plan?.conversions ?? []).map((c) => c.track_id));
                return (
                  <tr key={f.file_id} onDoubleClick={() => setDetail(f)} className={`hover:bg-slate-50 dark:hover:bg-slate-800/40 ${selected.has(f.file_id) ? "bg-sky-500/5" : ""}`}>
                    <td className="td"><input type="checkbox" checked={selected.has(f.file_id)} onChange={() => toggle(f.file_id)} /></td>
                    <td className="td max-w-[380px]"><div className="font-medium [overflow-wrap:anywhere]">{f.filename}</div><div className="text-[11px] text-slate-500">{f.directory || "/"}</div></td>
                    <td className="td whitespace-nowrap text-xs tabular-nums">{fmtBytes(f.size)}</td>
                    <td className="td"><div className="flex flex-wrap gap-1">{f.tracks.filter((t) => t.type === "audio").map((t) => <TrackChip key={t.id} t={t} convert={conv.has(t.id)} />)}</div>
                      {(f.plan?.conversions ?? []).map((c) => <div key={c.track_id} className="text-[11px] text-sky-500">#{c.track_id} → {c.track_name} ({f.plan?.mode === "add" ? "zusätzlich" : "ersetzt"})</div>)}
                    </td>
                    <td className="td">{f.plan?.changed ? <Saving bytes={f.plan.predicted_saved} unknown={f.plan.predicted_unknown} /> : <span className="text-xs text-slate-400">–</span>}</td>
                    <td className="td">{planBadge(f.plan, f.analyzed, f.ok)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {p && items.length === 0 && <Empty>Keine Dateien mit passenden Audiospuren. Ordner wählen und „Suchen“ klicken.</Empty>}
        <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-800"><Pagination page={page} pageSize={100} total={p?.total ?? 0} onPage={setPage} /></div>
      </div>
      <ContextMenu pos={menu} onClose={() => setMenu(null)} items={[
        { label: "Alle passenden Dateien markieren", action: () => setSelected(new Set(p?.changed_ids ?? [])) },
        { label: "Markierung aufheben", action: () => setSelected(new Set()) },
      ]} />
      {detail && <CommandModal fileId={detail.file_id} body={{ kind: "audio", profile_id: pid }} onClose={() => setDetail(null)} />}
      {confirm && (
        <Modal open title="Audiospuren konvertieren" onClose={() => setConfirm(false)}
          footer={<><button className="btn-secondary" onClick={() => setConfirm(false)}>Abbrechen</button><button className={mode.startsWith("replace") ? "btn-danger" : "btn-primary"} disabled={create.isPending} onClick={() => create.mutate()}>Konvertieren</button></>}>
          <div className="space-y-2 text-sm">
            <p><b>{fmtInt(selChanged)}</b> Datei(en) mit Profil „{profile?.name}“. FFmpeg codiert nur die betroffenen Spuren, mkvmerge setzt die Datei verlustfrei neu zusammen.</p>
            <p>Voraussichtliche Größenänderung: <Saving bytes={selPred.bytes} compact /> <span className="text-xs text-slate-500">(„+“ = Dateien werden größer, z. B. bei „zusätzlich“)</span></p>
            <p className={mode.startsWith("replace") ? "font-medium text-red-500" : ""}>{OUTPUT_MODE_HINT[mode]}</p>
          </div>
        </Modal>
      )}
    </div>
  );
}
