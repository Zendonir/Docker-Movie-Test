import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Download, RefreshCw, Search, X } from "lucide-react";
import { api, ApiError, post, qs } from "../api";
import { fmtBytes, fmtDate, fmtDuration, fmtInt } from "../format";
import type { FileStatus, Library, MediaFileSummary, Method, Paged } from "../types";
import { Card, Empty, ErrorBox, Loading, Modal, Pagination, STATUS_META, StatusBadge } from "../components/ui";
import { useToast } from "../components/toast";

interface Facets { extensions: { value: string; count: number }[]; directories: { value: string; count: number }[] }

const STATUSES: FileStatus[] = ["critical", "error", "warning", "incomplete", "ok", "unchecked"];
const COLUMNS: { key: string; label: string; sort?: string; className?: string }[] = [
  { key: "status", label: "Status", sort: "status" },
  { key: "name", label: "Filmname", sort: "name" },
  { key: "size", label: "Größe", sort: "size", className: "text-right" },
  { key: "duration", label: "Dauer", sort: "duration", className: "text-right" },
  { key: "video", label: "Videoformat", sort: "video" },
  { key: "last", label: "Letzte Prüfung", sort: "last_checked" },
  { key: "errors", label: "Fehler", sort: "errors", className: "text-right" },
  { key: "dir", label: "Verzeichnis", sort: "directory" },
];

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function RecheckModal({ open, onClose, fileIds, onDone }: { open: boolean; onClose: () => void; fileIds: number[]; onDone?: () => void }) {
  const toast = useToast();
  const [methods, setMethods] = useState<Method[]>(["container", "full"]);
  const [priority, setPriority] = useState(10);
  const m = useMutation({
    mutationFn: () => post<{ id: number }>("/api/jobs", { task: "files", methods, file_ids: fileIds, priority }),
    onSuccess: (j) => {
      toast(`Prüfauftrag #${j.id} angelegt`, "ok");
      onDone?.();
      onClose();
    },
    onError: (e: Error) => {
      const d = e instanceof ApiError ? (e.detail as { job_id?: number }) : undefined;
      toast(d?.job_id ? `Ein identischer Auftrag läuft bereits (#${d.job_id})` : e.message, "error");
    },
  });
  const toggle = (x: Method) => setMethods((ms) => (ms.includes(x) ? ms.filter((y) => y !== x) : [...ms, x]));
  return (
    <Modal open={open} onClose={onClose} title={`${fmtInt(fileIds.length)} Datei(en) erneut prüfen`}
      footer={<><button className="btn-secondary" onClick={onClose}>Abbrechen</button>
        <button className="btn-primary" disabled={!methods.length || m.isPending} onClick={() => m.mutate()}><RefreshCw className="h-4 w-4" />Prüfung starten</button></>}>
      <MethodPicker methods={methods} toggle={toggle} />
      <div className="mt-4">
        <label className="label">Priorität</label>
        <input type="number" className="input w-28" min={-100} max={100} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
        <p className="mt-1 text-xs text-slate-500">Höhere Werte werden zuerst bearbeitet.</p>
      </div>
    </Modal>
  );
}

export function MethodPicker({ methods, toggle }: { methods: Method[]; toggle: (m: Method) => void }) {
  const opts: { m: Method; title: string; text: string }[] = [
    { m: "quick", title: "A – Schnellprüfung", text: "Header, Streams, Laufzeit (FFprobe, mkvmerge -J). Keine vollständige Integritätsprüfung." },
    { m: "container", title: "B – Erweiterte Containerprüfung", text: "Liest die ganze Datei: EBML/Matroska-Struktur, Cluster, CRC, Zeitstempel, mkvmerge- und Demux-Durchlauf." },
    { m: "full", title: "C – Vollständige Prüfung", text: "Decodiert alle Video- und Audiostreams vollständig mit FFmpeg (rechenintensiv)." },
  ];
  return (
    <div className="space-y-2">
      {opts.map((o) => (
        <label key={o.m} className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${methods.includes(o.m) ? "border-sky-500 bg-sky-500/5" : "border-slate-200 dark:border-slate-700"}`}>
          <input type="checkbox" className="mt-1" checked={methods.includes(o.m)} onChange={() => toggle(o.m)} />
          <span>
            <span className="block text-sm font-medium">{o.title}</span>
            <span className="block text-xs text-slate-500">{o.text}</span>
          </span>
        </label>
      ))}
    </div>
  );
}

export function Files() {
  const [sp, setSp] = useSearchParams();
  const [search, setSearch] = useState(sp.get("q") ?? "");
  const debounced = useDebounced(search);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [recheck, setRecheck] = useState<number[] | null>(null);
  const toast = useToast();

  const params = useMemo(() => ({
    q: sp.get("q") ?? undefined,
    status: sp.getAll("status"),
    ext: sp.getAll("ext"),
    library_id: sp.get("library_id") ?? undefined,
    directory: sp.get("directory") ?? undefined,
    only_new: sp.get("only_new") === "1" ? true : undefined,
    only_damaged: sp.get("only_damaged") === "1" ? true : undefined,
    present: sp.get("present") ?? "yes",
    check_level: sp.getAll("check_level"),
  }), [sp]);
  const sort = sp.get("sort") ?? "name";
  const order = sp.get("order") ?? "asc";
  const page = Number(sp.get("page") ?? 1);
  const pageSize = Number(sp.get("page_size") ?? 50);

  const update = (changes: Record<string, string | string[] | null>, resetPage = true) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(changes)) {
      next.delete(k);
      if (Array.isArray(v)) v.forEach((x) => next.append(k, x));
      else if (v !== null && v !== "") next.set(k, v);
    }
    if (resetPage) next.delete("page");
    setSp(next, { replace: true });
  };

  useEffect(() => {
    if ((sp.get("q") ?? "") !== debounced) update({ q: debounced || null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);

  const files = useQuery({
    queryKey: ["files", params, sort, order, page, pageSize],
    queryFn: () => api<Paged<MediaFileSummary>>(`/api/files${qs({ ...params, sort, order, page, page_size: pageSize })}`),
    placeholderData: keepPreviousData,
  });
  const facets = useQuery({ queryKey: ["facets", params.library_id], queryFn: () => api<Facets>(`/api/files/facets${qs({ library_id: params.library_id })}`), staleTime: 60000 });
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<Library[]>("/api/libraries"), staleTime: 60000 });

  const items = files.data?.items ?? [];
  const allOnPage = items.length > 0 && items.every((f) => selected.has(f.id));
  const toggleAll = () => {
    const n = new Set(selected);
    if (allOnPage) items.forEach((f) => n.delete(f.id));
    else items.forEach((f) => n.add(f.id));
    setSelected(n);
  };
  const toggleOne = (id: number) => {
    const n = new Set(selected);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    setSelected(n);
  };
  const selectAllMatches = async () => {
    try {
      const r = await api<{ ids: number[]; count: number }>(`/api/files/ids${qs(params)}`, { method: "POST" });
      setSelected(new Set(r.ids));
      toast(`${fmtInt(r.count)} Dateien ausgewählt`, "info");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const setSort = (s: string) => update({ sort: s, order: sort === s && order === "asc" ? "desc" : "asc" }, false);
  const exportUrl = (format: string) => `/api/export${qs({ format, scope: "filter", ...params })}`;
  const statusSel = params.status;
  const activeFilters = [...statusSel, ...params.ext].length + (params.directory ? 1 : 0) + (params.only_new ? 1 : 0) + (params.only_damaged ? 1 : 0) + (params.q ? 1 : 0) + (params.library_id ? 1 : 0) + params.check_level.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Dateien</h1>
          <p className="text-sm text-slate-500">{fmtInt(files.data?.total)} Treffer</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <a className="btn-secondary" href={exportUrl("csv")}><Download className="h-4 w-4" />CSV</a>
          <a className="btn-secondary" href={exportUrl("json")}><Download className="h-4 w-4" />JSON</a>
          <a className="btn-secondary" href={exportUrl("html")}><Download className="h-4 w-4" />HTML</a>
        </div>
      </div>

      <Card>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-slate-400" />
              <input className="input pl-8" placeholder="Suche in Dateiname und Pfad …" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <select className="input w-auto" value={params.library_id ?? ""} onChange={(e) => update({ library_id: e.target.value || null, directory: null })}>
              <option value="">Alle Bibliotheken</option>
              {libs.data?.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
            <select className="input w-auto" value={params.ext[0] ?? ""} onChange={(e) => update({ ext: e.target.value ? [e.target.value] : [] })}>
              <option value="">Alle Formate</option>
              {facets.data?.extensions.map((x) => <option key={x.value} value={x.value}>.{x.value} ({fmtInt(x.count)})</option>)}
            </select>
            <select className="input w-auto max-w-[260px]" value={params.directory ?? ""} onChange={(e) => update({ directory: e.target.value || null })}>
              <option value="">Alle Verzeichnisse</option>
              {facets.data?.directories.map((x) => <option key={x.value} value={x.value}>{x.value} ({fmtInt(x.count)})</option>)}
            </select>
            <select className="input w-auto" value={params.present} onChange={(e) => update({ present: e.target.value === "yes" ? null : e.target.value })}>
              <option value="yes">Vorhandene Dateien</option>
              <option value="no">Nicht mehr vorhanden</option>
              <option value="all">Alle</option>
            </select>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {STATUSES.map((st) => {
              const on = statusSel.includes(st);
              return (
                <button key={st} onClick={() => update({ status: on ? statusSel.filter((x) => x !== st) : [...statusSel, st] })}
                  className={`rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset transition ${on ? STATUS_META[st].cls : "text-slate-500 ring-slate-300 hover:bg-slate-100 dark:ring-slate-700 dark:hover:bg-slate-800"}`}>
                  {STATUS_META[st].label}
                </button>
              );
            })}
            <span className="mx-1 h-5 w-px bg-slate-300 dark:bg-slate-700" />
            <label className="flex items-center gap-1.5 text-sm"><input type="checkbox" checked={!!params.only_new} onChange={(e) => update({ only_new: e.target.checked ? "1" : null })} />Nur neue/ungeprüfte</label>
            <label className="flex items-center gap-1.5 text-sm"><input type="checkbox" checked={!!params.only_damaged} onChange={(e) => update({ only_damaged: e.target.checked ? "1" : null })} />Nur beschädigte</label>
            {activeFilters > 0 && (
              <button className="btn-ghost text-xs" onClick={() => { setSearch(""); setSp(new URLSearchParams(), { replace: true }); }}><X className="h-3.5 w-3.5" />Filter zurücksetzen</button>
            )}
          </div>
        </div>
      </Card>

      {selected.size > 0 && (
        <div className="sticky top-16 z-10 flex flex-wrap items-center gap-2 rounded-lg border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm backdrop-blur">
          <span className="font-medium">{fmtInt(selected.size)} ausgewählt</span>
          <button className="btn-primary" onClick={() => setRecheck([...selected])}><RefreshCw className="h-4 w-4" />Erneut prüfen</button>
          <button className="btn-ghost" onClick={selectAllMatches}>Alle {fmtInt(files.data?.total)} Treffer auswählen</button>
          <button className="btn-ghost" onClick={() => setSelected(new Set())}>Auswahl aufheben</button>
        </div>
      )}

      <div className="card overflow-hidden">
        {files.error && <div className="p-4"><ErrorBox error={files.error} /></div>}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[960px]">
            <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
              <tr>
                <th className="th w-8"><input type="checkbox" checked={allOnPage} onChange={toggleAll} aria-label="Alle auf dieser Seite auswählen" /></th>
                {COLUMNS.map((c) => (
                  <th key={c.key} className={`th ${c.className ?? ""}`}>
                    {c.sort ? (
                      <button className="inline-flex items-center gap-1 hover:text-sky-500" onClick={() => setSort(c.sort!)}>
                        {c.label}
                        {sort === c.sort && (order === "asc" ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />)}
                      </button>
                    ) : c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((f) => (
                <tr key={f.id} className={`hover:bg-slate-50 dark:hover:bg-slate-800/40 ${selected.has(f.id) ? "bg-sky-500/5" : ""} ${files.isPlaceholderData ? "opacity-60" : ""}`}>
                  <td className="td"><input type="checkbox" checked={selected.has(f.id)} onChange={() => toggleOne(f.id)} aria-label={`${f.filename} auswählen`} /></td>
                  <td className="td">
                    <StatusBadge status={f.status} small />
                    {f.status === "ok" && !f.methods_done.includes("full") && <div className="mt-0.5 text-[10px] text-slate-500">ohne Decodierung</div>}
                    {!f.present && <div className="mt-0.5 text-[10px] text-red-500">nicht vorhanden</div>}
                  </td>
                  <td className="td max-w-[420px]">
                    <Link to={`/dateien/${f.id}`} className="font-medium hover:text-sky-500 [overflow-wrap:anywhere]">{f.filename}</Link>
                    <div className="text-[11px] text-slate-500">{f.check_level_label}</div>
                  </td>
                  <td className="td text-right tabular-nums">{fmtBytes(f.size)}</td>
                  <td className="td text-right tabular-nums">{fmtDuration(f.duration)}</td>
                  <td className="td">{f.video_codec ? <>{f.video_codec}<div className="text-[11px] text-slate-500">{f.video_resolution}</div></> : "–"}</td>
                  <td className="td whitespace-nowrap text-xs">{fmtDate(f.last_checked_at, true)}</td>
                  <td className="td text-right tabular-nums">
                    {f.error_count > 0 ? <span className="font-semibold text-orange-500">{f.error_count}</span> : "0"}
                    {f.warning_count > 0 && <div className="text-[11px] text-amber-500">{f.warning_count} Warn.</div>}
                  </td>
                  <td className="td max-w-[260px] text-xs text-slate-500 [overflow-wrap:anywhere]">{f.library ? `${f.library}/` : ""}{f.directory || "/"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {files.isLoading && <Loading />}
        {!files.isLoading && items.length === 0 && <Empty>Keine Dateien gefunden.</Empty>}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          <select className="input w-auto" value={pageSize} onChange={(e) => update({ page_size: e.target.value })}>
            {[25, 50, 100, 250, 500].map((n) => <option key={n} value={n}>{n} pro Seite</option>)}
          </select>
          <Pagination page={page} pageSize={pageSize} total={files.data?.total ?? 0} onPage={(p) => update({ page: String(p) }, false)} />
        </div>
      </div>
      {recheck && <RecheckModal open onClose={() => setRecheck(null)} fileIds={recheck} onDone={() => setSelected(new Set())} />}
    </div>
  );
}
