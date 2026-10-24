import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, FolderSearch, Plus, Trash2 } from "lucide-react";
import { api, del, patch, post, qs } from "../api";
import { fmtBytes, fmtDate, fmtInt } from "../format";
import type { Library } from "../types";
import { Card, Empty, ErrorBox, Loading, Modal, ProgressBar, Toggle } from "../components/ui";
import { useToast } from "../components/toast";

interface Browse { path: string; parent: string | null; directories: { name: string; path: string }[] }
interface Root { path: string; exists: boolean; storage: { total: number; free: number; used: number } | null }

function DirPicker({ value, onPick }: { value: string; onPick: (p: string) => void }) {
  const roots = useQuery({ queryKey: ["roots"], queryFn: () => api<Root[]>("/api/libraries/roots") });
  const start = value || roots.data?.[0]?.path || "";
  const b = useQuery({ queryKey: ["browse", start], queryFn: () => api<Browse>(`/api/libraries/browse${qs({ path: start })}`), enabled: !!start });
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700">
      <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 text-xs dark:border-slate-700">
        <span className="font-mono [overflow-wrap:anywhere]">{b.data?.path ?? start}</span>
        {b.data?.parent && <button className="btn-ghost text-xs" onClick={() => onPick(b.data!.parent!)}>↑ übergeordnet</button>}
      </div>
      <ul className="max-h-56 overflow-y-auto text-sm">
        {b.data?.directories.map((d) => (
          <li key={d.path}><button className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => onPick(d.path)}><FolderOpen className="h-4 w-4 text-sky-500" />{d.name}</button></li>
        ))}
        {b.data && b.data.directories.length === 0 && <li className="px-3 py-2 text-xs text-slate-500">Keine Unterverzeichnisse</li>}
      </ul>
      {b.error && <div className="p-2"><ErrorBox error={b.error} /></div>}
    </div>
  );
}

function AddLibrary({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [excl, setExcl] = useState("");
  const qc = useQueryClient();
  const toast = useToast();
  const m = useMutation({
    mutationFn: () => post("/api/libraries", { name, path, exclude_patterns: excl.split("\n").map((s) => s.trim()).filter(Boolean) }),
    onSuccess: () => { toast("Bibliothek angelegt", "ok"); qc.invalidateQueries({ queryKey: ["libraries"] }); qc.invalidateQueries({ queryKey: ["stats"] }); onClose(); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  return (
    <Modal open title="Bibliothek hinzufügen" onClose={onClose} wide
      footer={<><button className="btn-secondary" onClick={onClose}>Abbrechen</button><button className="btn-primary" disabled={!name || !path || m.isPending} onClick={() => m.mutate()}>Anlegen</button></>}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <div><label className="label">Name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="z. B. Filme" /></div>
          <div><label className="label">Pfad im Container</label><input className="input font-mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/media/Filme" /></div>
          <div><label className="label">Ausschlussmuster (je Zeile, zusätzlich zu den globalen)</label><textarea className="input h-24 font-mono" value={excl} onChange={(e) => setExcl(e.target.value)} placeholder={"Extras\n*/Trailer/*\n*sample*"} /></div>
          <p className="text-xs text-slate-500">Nur Pfade innerhalb der eingebundenen Medienverzeichnisse sind erlaubt. Symlinks aus der Bibliothek hinaus werden nicht verfolgt.</p>
        </div>
        <div><label className="label">Verzeichnis auswählen</label><DirPicker value={path} onPick={(p) => { setPath(p); if (!name) setName(p.split("/").filter(Boolean).pop() ?? ""); }} /></div>
      </div>
    </Modal>
  );
}

function LibraryCard({ l }: { l: Library }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [excl, setExcl] = useState((l.exclude_patterns ?? []).join("\n"));
  const refresh = () => { qc.invalidateQueries({ queryKey: ["libraries"] }); qc.invalidateQueries({ queryKey: ["stats"] }); qc.invalidateQueries({ queryKey: ["files"] }); };
  const inv = useMutation({ mutationFn: () => post<Record<string, number>>(`/api/libraries/${l.id}/inventory`), onSuccess: (s) => { toast(`Bestandsaufnahme: ${s.files_found} Dateien, ${s.new} neu, ${s.changed} geändert, ${s.moved} verschoben, ${s.missing} fehlen`, "ok"); refresh(); }, onError: (e: Error) => toast(e.message, "error") });
  const upd = useMutation({ mutationFn: (b: Record<string, unknown>) => patch(`/api/libraries/${l.id}`, b), onSuccess: () => { toast("Gespeichert", "ok"); refresh(); }, onError: (e: Error) => toast(e.message, "error") });
  const rm = useMutation({ mutationFn: () => del(`/api/libraries/${l.id}`), onSuccess: () => { toast("Bibliothek entfernt (Dateien unverändert)", "ok"); refresh(); } });
  const st = l.storage;
  const stats = l.last_inventory_stats as Record<string, number> | null;
  return (
    <Card title={<span className="flex items-center gap-2">{l.name}{!l.reachable && <span className="rounded bg-red-500/15 px-1.5 text-xs text-red-500">nicht erreichbar</span>}</span>}
      actions={<>
        <button className="btn-secondary" onClick={() => inv.mutate()} disabled={inv.isPending}><FolderSearch className="h-4 w-4" />Bestandsaufnahme</button>
        <button className="btn-ghost text-red-500" onClick={() => confirm(`Bibliothek „${l.name}“ und alle zugehörigen Prüfergebnisse aus der Datenbank entfernen? Die Mediendateien selbst bleiben unverändert.`) && rm.mutate()}><Trash2 className="h-4 w-4" /></button>
      </>}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2 text-sm">
          <div className="font-mono text-xs text-slate-500 [overflow-wrap:anywhere]">{l.path}</div>
          <div>{fmtInt(l.file_count)} Dateien · {fmtBytes(l.total_size)}</div>
          {st && <><ProgressBar value={st.total ? st.used / st.total : 0} /><div className="text-xs text-slate-500">{fmtBytes(st.used)} belegt · {fmtBytes(st.free)} frei · {fmtBytes(st.total)} gesamt</div></>}
          <div className="text-xs text-slate-500">Letzte Bestandsaufnahme: {fmtDate(l.last_inventory_at)}{stats && ` – ${fmtInt(stats.files_found)} gefunden, ${fmtInt(stats.excluded)} ausgeschlossen, ${fmtInt(stats.symlinks_skipped)} Symlinks ignoriert`}</div>
          <Toggle checked={l.enabled} onChange={(v) => upd.mutate({ enabled: v })} label="Aktiv" hint="Inaktive Bibliotheken werden bei Scans übersprungen." />
        </div>
        <div>
          <label className="label">Ausschlussmuster</label>
          <textarea className="input h-24 font-mono text-xs" value={excl} onChange={(e) => setExcl(e.target.value)} />
          <button className="btn-secondary mt-2" onClick={() => upd.mutate({ exclude_patterns: excl.split("\n").map((s) => s.trim()).filter(Boolean) })}>Muster speichern</button>
        </div>
      </div>
    </Card>
  );
}

export function Libraries() {
  const q = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/api/libraries") });
  const [add, setAdd] = useState(false);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div><h1 className="text-xl font-semibold">Bibliotheken</h1><p className="text-sm text-slate-500">Medienordner, die rekursiv durchsucht werden. Unterstützt: MKV, MP4, AVI, MOV, M4V, TS.</p></div>
        <button className="btn-primary" onClick={() => setAdd(true)}><Plus className="h-4 w-4" />Bibliothek hinzufügen</button>
      </div>
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : q.data!.length === 0 ? <Card><Empty>Noch keine Bibliothek eingerichtet.</Empty></Card> : q.data!.map((l) => <LibraryCard key={l.id} l={l} />)}
      {add && <AddLibrary onClose={() => setAdd(false)} />}
    </div>
  );
}
