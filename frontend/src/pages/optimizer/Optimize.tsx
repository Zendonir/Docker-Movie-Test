import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { ApiError, post } from "../../api";
import { fmtBytes, fmtInt } from "../../format";
import type { Choice, OptFile, OptPreview, OptRules, TrackRule } from "../../types";
import { Card, Empty, ErrorBox, Loading, Modal, Pagination } from "../../components/ui";
import { useToast } from "../../components/toast";
import { CommandModal, ContextMenu, Folder, FolderPicker, OUTPUT_MODE_HINT, Saving, SearchButton, TagInput, TrackChip, planBadge, sumPredictions, useOptStatus } from "./common";

const EMPTY_RULE: TrackRule = { mode: "language", languages: [], codecs: [], track_ids: [] };

function RuleEditor({ title, rule, onChange, langs, codecs, hintEmpty }: {
  title: string; rule: TrackRule; onChange: (r: TrackRule) => void; langs: Choice[]; codecs: Choice[]; hintEmpty: string;
}) {
  const [ids, setIds] = useState(rule.track_ids.join(", "));
  useEffect(() => setIds(rule.track_ids.join(", ")), [rule.track_ids]);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="flex gap-3 text-xs">
          <label className="flex items-center gap-1"><input type="radio" checked={rule.mode === "language"} onChange={() => onChange({ ...rule, mode: "language" })} />Sprache</label>
          <label className="flex items-center gap-1"><input type="radio" checked={rule.mode === "track_id"} onChange={() => onChange({ ...rule, mode: "track_id" })} />Track-ID</label>
        </div>
      </div>
      {rule.mode === "language" ? (
        <>
          <div>
            <label className="label">Spuren behalten mit folgender Sprache</label>
            <TagInput values={rule.languages} onChange={(v) => onChange({ ...rule, languages: v })} suggestions={langs} placeholder="z. B. ger, eng" />
            <p className="mt-1 text-xs text-slate-500">{rule.languages.length ? "Reihenfolge = Reihenfolge der Spuren in der neuen Datei." : hintEmpty}</p>
          </div>
          <div>
            <label className="label">Nur folgende Codecs übernehmen (bei mehreren Spuren gleicher Sprache)</label>
            <TagInput values={rule.codecs} onChange={(v) => onChange({ ...rule, codecs: v })} suggestions={codecs} placeholder="leer = alle, z. B. DTS" upper />
          </div>
        </>
      ) : (
        <div>
          <label className="label">Track-IDs behalten (durch Komma getrennt)</label>
          <input className="input font-mono" value={ids} placeholder="z. B. 1, 3"
            onChange={(e) => setIds(e.target.value)}
            onBlur={() => onChange({ ...rule, track_ids: ids.split(/[,\s]+/).map((x) => parseInt(x, 10)).filter((x) => !isNaN(x)) })} />
          <p className="mt-1 text-xs text-slate-500">Die Track-IDs entsprechen mkvmerge (Video meist 0). Sinnvoll, wenn alle gewählten Dateien gleich aufgebaut sind.</p>
        </div>
      )}
    </div>
  );
}

export function OptimizeTab() {
  const status = useOptStatus();
  const toast = useToast();
  const nav = useNavigate();
  const [folder, setFolder] = useState<Folder>({ library_id: null, directory: "", recursive: true });
  const [onlyMulti, setOnlyMulti] = useState(true);
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [rules, setRules] = useState<OptRules | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<OptFile | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [debounced, setDebounced] = useState<OptRules | null>(null);
  const prefilled = useRef<string>("");

  useEffect(() => { if (status.data && !rules) setRules(status.data.last_rules); }, [status.data, rules]);
  useEffect(() => { const t = setTimeout(() => setDebounced(rules), 300); return () => clearTimeout(t); }, [rules]);

  const body = { library_id: folder.library_id, directory: folder.directory, recursive: folder.recursive, only_multi_audio: onlyMulti, kind: "remux", rules: debounced, only_changed: onlyChanged, page, page_size: 100 };
  const preview = useQuery({
    queryKey: ["opt-preview", "remux", body], queryFn: () => post<OptPreview>("/api/optimizer/preview", body),
    enabled: folder.library_id != null && debounced != null, placeholderData: keepPreviousData,
  });
  const p = preview.data;

  // Nach dem Suchen: gefundene Sprachen ins Feld übernehmen (nicht gewünschte einfach löschen)
  useEffect(() => {
    if (!p || !rules) return;
    const key = `${folder.library_id}|${folder.directory}|${p.total}|${p.unanalyzed}`;
    if (prefilled.current === key || p.unanalyzed > 0 || p.total === 0) return;
    prefilled.current = key;
    if (rules.audio.languages.length === 0) {
      setRules({
        audio: { ...rules.audio, languages: p.choices.audio_languages.map((c) => c.value) },
        subtitles: rules.subtitles.languages.length ? rules.subtitles : { ...rules.subtitles, languages: p.choices.subtitle_languages.map((c) => c.value) },
      });
    }
  }, [p, rules, folder]);

  const create = useMutation({
    mutationFn: () => post<{ id: number }>("/api/optimizer/jobs", { kind: "remux", file_ids: [...selected], rules }),
    onSuccess: (j) => { toast(`Auftrag #${j.id} gestartet`, "ok"); setSelected(new Set()); setConfirm(false); nav("/optimizer/auftraege"); },
    onError: (e: Error) => { toast(e instanceof ApiError ? e.message : String(e), "error"); setConfirm(false); },
  });

  const items = p?.items ?? [];
  const toggle = (id: number) => { const n = new Set(selected); if (n.has(id)) n.delete(id); else n.add(id); setSelected(n); };
  const selectChanged = () => setSelected(new Set(p?.changed_ids ?? []));
  const selectAllPage = () => setSelected(new Set([...selected, ...items.map((i) => i.file_id)]));
  const selectedChanged = useMemo(() => [...selected].filter((id) => p?.changed_ids.includes(id)).length, [selected, p]);
  const mode = status.data?.ops.output_mode ?? "output_dir";
  const selPred = useMemo(() => sumPredictions(selected, p?.predictions), [selected, p]);

  if (status.isLoading || !rules) return <Loading />;
  const choices = p?.choices;
  return (
    <div className="space-y-4">
      <Card title="Ordner durchsuchen">
        <div className="space-y-3">
          <FolderPicker value={folder} onChange={(f) => { setFolder(f); setSelected(new Set()); setPage(1); }}>
            <label className="flex items-center gap-1.5 pb-2 text-sm"><input type="checkbox" checked={onlyMulti} onChange={(e) => setOnlyMulti(e.target.checked)} />Nur Dateien mit mehr als einer Audiospur</label>
          </FolderPicker>
          <SearchButton folder={folder} />
        </div>
      </Card>

      <Card title="Regeln" actions={<>
        <button className="btn-ghost text-xs" onClick={() => choices && setRules({ audio: { ...rules.audio, languages: choices.audio_languages.map((c) => c.value) }, subtitles: { ...rules.subtitles, languages: choices.subtitle_languages.map((c) => c.value) } })}>Gefundene Sprachen übernehmen</button>
        <button className="btn-ghost text-xs" onClick={() => status.data && setRules(status.data.last_rules)}>Zuletzt verwendete Regeln</button>
      </>}>
        <div className="grid gap-6 lg:grid-cols-2">
          <RuleEditor title="Audiospuren" rule={rules.audio} onChange={(r) => setRules({ ...rules, audio: r })}
            langs={choices?.audio_languages ?? []} codecs={choices?.audio_codecs ?? []}
            hintEmpty="Ohne Sprache bleiben nur geschützte Sprachen erhalten. Eine Datei wird nie ohne Audiospur geschrieben." />
          <RuleEditor title="Untertitel" rule={rules.subtitles ?? EMPTY_RULE} onChange={(r) => setRules({ ...rules, subtitles: r })}
            langs={choices?.subtitle_languages ?? []} codecs={choices?.subtitle_codecs ?? []}
            hintEmpty="Leer = alle Untertitel entfernen (außer geschützte Sprachen)." />
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Geschützte Sprachen (nie vollständig entfernt): {(status.data?.ops.protected_languages ?? []).map((x) => x.toUpperCase()).join(", ") || "keine"} ·
          Ausgabe: {OUTPUT_MODE_HINT[mode]} (änderbar unter „Einstellungen“)
        </p>
      </Card>

      <div className="card overflow-hidden" onContextMenu={(e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); }}>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <div className="text-sm">
            <b>{fmtInt(p?.total ?? 0)}</b> Dateien · <span className="text-emerald-600 dark:text-emerald-400">{fmtInt(p?.changed_ids.length ?? 0)} mit Änderungen</span>
            {p && p.unanalyzed > 0 && <span className="text-amber-500"> · {fmtInt(p.unanalyzed)} noch nicht analysiert</span>}
            {selected.size > 0 && <> · <b>{fmtInt(selected.size)}</b> markiert</>}
            {p && p.changed_ids.length > 0 && (
              <div className="mt-0.5 text-xs text-slate-500">
                Voraussichtlich eingespart: <Saving bytes={p.prediction.saved} exact={p.prediction.exact} compact /> bei allen Dateien mit Änderungen
                {selected.size > 0 && <> · markiert: <Saving bytes={selPred.bytes} compact /></>}
                {p.prediction.files_with_unknown > 0 && <> · {fmtInt(p.prediction.files_with_unknown)} Datei(en) nur teilweise vorhersagbar</>}
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={onlyChanged} onChange={(e) => { setOnlyChanged(e.target.checked); setPage(1); }} />nur Dateien mit Änderungen</label>
            <button className="btn-secondary" onClick={selectChanged}>Alle mit Änderungen markieren</button>
            <button className="btn-ghost" onClick={() => setSelected(new Set())}>Keine</button>
            <button className="btn-primary" disabled={selectedChanged === 0} onClick={() => setConfirm(true)}><Play className="h-4 w-4" />Konvertieren ({fmtInt(selectedChanged)})</button>
          </div>
        </div>
        <ErrorBox error={preview.error} />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px]">
            <thead className="bg-slate-50 dark:bg-slate-900/60"><tr>
              <th className="th w-8"><input type="checkbox" checked={items.length > 0 && items.every((i) => selected.has(i.file_id))} onChange={(e) => e.target.checked ? selectAllPage() : setSelected(new Set([...selected].filter((id) => !items.some((i) => i.file_id === id))))} /></th>
              <th className="th">Datei</th><th className="th">Größe</th><th className="th">Audio</th><th className="th">Untertitel</th><th className="th">Ersparnis</th><th className="th">Ergebnis</th>
            </tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((f) => {
                const keep = new Map((f.plan?.decisions ?? []).map((d) => [d.track_id, d.keep]));
                const show = (id: number) => (f.plan?.changed ? keep.get(id) : undefined);
                return (
                  <tr key={f.file_id} onDoubleClick={() => setDetail(f)} className={`cursor-default hover:bg-slate-50 dark:hover:bg-slate-800/40 ${selected.has(f.file_id) ? "bg-sky-500/5" : ""}`} title="Doppelklick: Spuren und Befehl anzeigen">
                    <td className="td"><input type="checkbox" checked={selected.has(f.file_id)} onChange={() => toggle(f.file_id)} /></td>
                    <td className="td max-w-[360px]"><div className="font-medium [overflow-wrap:anywhere]">{f.filename}</div><div className="text-[11px] text-slate-500">{f.directory || "/"}</div></td>
                    <td className="td whitespace-nowrap text-xs tabular-nums">{fmtBytes(f.size)}</td>
                    <td className="td"><div className="flex flex-wrap gap-1">{f.tracks.filter((t) => t.type === "audio").map((t) => <TrackChip key={t.id} t={t} keep={show(t.id)} />)}</div></td>
                    <td className="td"><div className="flex flex-wrap gap-1">{f.tracks.filter((t) => t.type === "subtitles").map((t) => <TrackChip key={t.id} t={t} keep={show(t.id)} />)}</div></td>
                    <td className="td">{f.plan?.changed ? <Saving bytes={f.plan.predicted_saved} exact={f.plan.predicted_exact} unknown={f.plan.predicted_unknown} /> : <span className="text-xs text-slate-400">–</span>}</td>
                    <td className="td">{planBadge(f.plan, f.analyzed, f.ok)}{f.status === "critical" || f.status === "error" ? <div className="text-[11px] text-red-500">Integritätsfehler</div> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {preview.isLoading && <Loading />}
        {p && items.length === 0 && <Empty>Keine passenden MKV-Dateien. Ordner wählen und „Suchen“ klicken.</Empty>}
        <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-800"><Pagination page={page} pageSize={100} total={p?.total ?? 0} onPage={setPage} /></div>
      </div>

      <ContextMenu pos={menu} onClose={() => setMenu(null)} items={[
        { label: "Alle Dateien mit Änderungen markieren", action: selectChanged },
        { label: "Alle auf dieser Seite markieren", action: selectAllPage },
        { label: "Markierung aufheben", action: () => setSelected(new Set()) },
      ]} />
      {detail && <CommandModal fileId={detail.file_id} body={{ kind: "remux", rules }} onClose={() => setDetail(null)} />}
      {confirm && (
        <Modal open title="Spuren entfernen" onClose={() => setConfirm(false)}
          footer={<><button className="btn-secondary" onClick={() => setConfirm(false)}>Abbrechen</button><button className={mode.startsWith("replace") ? "btn-danger" : "btn-primary"} disabled={create.isPending} onClick={() => create.mutate()}>Konvertieren</button></>}>
          <div className="space-y-2 text-sm">
            <p><b>{fmtInt(selectedChanged)}</b> Datei(en) werden mit mkvmerge neu geschrieben (ohne Neucodierung).</p>
            <p>Voraussichtliche Ersparnis: <Saving bytes={selPred.bytes} compact />{selPred.unknown > 0 && <span className="text-xs text-slate-500"> (für {selPred.unknown} Datei(en) nicht vorhersagbar)</span>}{mode === "output_dir" || mode === "beside" ? <span className="text-xs text-slate-500"> – wird erst frei, wenn die Originale ersetzt werden.</span> : null}</p>
            <p className={mode.startsWith("replace") ? "font-medium text-red-500" : ""}>{OUTPUT_MODE_HINT[mode]}</p>
            <p className="text-xs text-slate-500">Jede neue Datei wird vor dem Abschluss geprüft. Dateien mit Integritätsfehlern werden übersprungen (abschaltbar).</p>
          </div>
        </Modal>
      )}
    </div>
  );
}
