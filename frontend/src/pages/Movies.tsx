import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Download, ExternalLink, RefreshCw, Search, Star } from "lucide-react";
import { api, del, post, put, qs } from "../api";
import { fmtBytes, fmtDate, fmtDuration, fmtInt } from "../format";
import type { FileStatus } from "../types";
import { Card, Empty, ErrorBox, Loading, Modal, Pagination, ProgressBar, StatusBadge } from "../components/ui";
import { useToast } from "../components/toast";

interface Audio { index: number; language: string; language_label: string; codec: string | null; profile: string | null; channels: number | null; channel_layout: string | null; title: string | null; default: boolean; bit_rate: number | null }
interface Sub { index: number; language: string; codec: string | null; forced: boolean; title: string | null; default: boolean }
interface Movie {
  file_id: number; filename: string; directory: string; library: string | null; path: string; size: number; duration: number | null;
  bit_rate: number | null; container: string | null; status: FileStatus; status_label: string; added: string; probed: boolean;
  title: string; original_title: string | null; parsed_title: string | null; year: number | null; imdb_id: string | null; imdb_url: string | null;
  rating: number | null; votes: number | null; runtime_min: number | null; genres: { value: string; label: string }[];
  match_source: string | null; match_confidence: string | null; manual: boolean;
  video: { codec: string | null; profile: string | null; width: number | null; height: number | null; fps: number | null; bit_depth: number | null; pix_fmt: string | null } | null;
  audio: Audio[]; subtitles: Sub[];
}
interface MovieList { total: number; items: Movie[]; pending_matches: number }
interface Facet { value: string; label: string; count: number }
interface Facets { genres: Facet[]; audio_languages: Facet[]; subtitle_languages: Facet[]; total: number; matched: number; rated: number; unprobed: number; average_rating: number | null }
interface ImdbStatus {
  available: boolean; meta: Record<string, string>; files: Record<string, boolean>; data_dir: string; attribution: string;
  state: { running: boolean; phase: string | null; rows: number; bytes_done: number; bytes_total: number | null; message: string | null; error: string | null };
  match: { running: boolean; total: number; done: number; matched: number; message: string | null };
}
interface Candidate { imdb_id: string; title: string; title_de: string | null; original_title: string; year: number | null; type: string; runtime: number | null; genres: string | null; rating: number | null; votes: number | null }

const COLUMNS: { key: string; label: string; sort?: string; cls?: string }[] = [
  { key: "expand", label: "" },
  { key: "title", label: "Titel", sort: "title" },
  { key: "year", label: "Jahr", sort: "year" },
  { key: "rating", label: "IMDb", sort: "rating" },
  { key: "runtime", label: "Laufzeit", sort: "runtime", cls: "text-right" },
  { key: "genres", label: "Genres", sort: "genres" },
  { key: "video", label: "Video", sort: "resolution" },
  { key: "audio", label: "Tonspuren", sort: "audio" },
  { key: "subs", label: "Untertitel" },
  { key: "size", label: "Größe", sort: "size", cls: "text-right" },
  { key: "status", label: "Zustand", sort: "status" },
  { key: "added", label: "Hinzugefügt", sort: "added" },
];

const CH: Record<number, string> = { 1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1" };
const CODEC: Record<string, string> = { ac3: "AC3", eac3: "E-AC3", dts: "DTS", truehd: "TrueHD", aac: "AAC", flac: "FLAC", opus: "Opus", mp3: "MP3", vorbis: "Vorbis", pcm_s16le: "PCM", pcm_s24le: "PCM", h264: "H.264", hevc: "HEVC", av1: "AV1", mpeg2video: "MPEG-2", vp9: "VP9", mpeg4: "MPEG-4", subrip: "SRT", ass: "ASS", hdmv_pgs_subtitle: "PGS", dvd_subtitle: "VobSub" };
const SOURCE: Record<string, string> = { manual: "manuell", nfo: ".nfo-Datei", filename: "IMDb-ID im Dateinamen", title_year: "Titel + Jahr" };
const CONF: Record<string, string> = { exact: "eindeutig", high: "sicher", medium: "wahrscheinlich", low: "unsicher (ohne Jahr)", ambiguous: "mehrdeutig – bitte zuordnen", none: "keine Zuordnung" };

function audioLabel(a: Audio): string {
  const codec = a.profile && /DTS-HD|TrueHD|Atmos|MA/i.test(a.profile) ? a.profile.replace("DTS-HD MA", "DTS-HD MA") : (CODEC[a.codec ?? ""] ?? a.codec ?? "?");
  return `${a.language.toUpperCase()} ${codec}${a.channels ? " " + (CH[a.channels] ?? `${a.channels}ch`) : ""}`;
}

function runtime(m: Movie): string {
  const sec = m.duration ?? (m.runtime_min ? m.runtime_min * 60 : null);
  if (sec == null) return "–";
  if (sec < 60) return `${Math.round(sec)} s`;
  const h = Math.floor(sec / 3600);
  const min = Math.round((sec % 3600) / 60);
  return h ? `${h}:${String(min).padStart(2, "0")} h` : `${min} min`;
}

function resLabel(v: Movie["video"]): string {
  if (!v?.height) return "–";
  const h = v.height, w = v.width ?? 0;
  if (h >= 1700 || w >= 3500) return "4K";
  if (h >= 1000 || w >= 1900) return "1080p";
  if (h >= 700 || w >= 1260) return "720p";
  return `${h}p`;
}

function Rating({ m }: { m: Movie }) {
  if (m.rating == null) return <span className="text-xs text-slate-400">–</span>;
  const color = m.rating >= 8 ? "text-emerald-500" : m.rating >= 6.5 ? "text-amber-400" : "text-orange-500";
  return (
    <span className="inline-flex flex-col">
      <span className={`inline-flex items-center gap-1 font-semibold tabular-nums ${color}`}><Star className="h-3.5 w-3.5 fill-current" />{m.rating.toLocaleString("de-DE", { minimumFractionDigits: 1 })}</span>
      <span className="text-[10px] text-slate-500 tabular-nums">{fmtInt(m.votes)} Stimmen</span>
    </span>
  );
}

function ImdbPanel({ status, onChanged }: { status?: ImdbStatus; onChanged: () => void }) {
  const toast = useToast();
  const [akas, setAkas] = useState(true);
  const update = useMutation({
    mutationFn: (download: boolean) => post("/api/movies/imdb/update", { download, include_akas: akas }),
    onSuccess: () => { toast("IMDb-Aktualisierung gestartet", "info"); onChanged(); },
    onError: (e: Error) => toast(e.message, "error"),
  });
  if (!status) return null;
  const st = status.state;
  const busy = st.running || status.match.running;
  return (
    <Card title="IMDb-Bewertungen">
      <div className="space-y-2 text-sm">
        {status.available ? (
          <p>Lokale IMDb-Daten vom <b>{status.meta.dataset_date}</b> ({fmtInt(Number(status.meta.titles))} Filme, {fmtInt(Number(status.meta.akas))} deutsche Titel). Die Zuordnung läuft komplett lokal.</p>
        ) : (
          <p>Für Bewertungen werden die offiziellen IMDb-Datensätze einmalig heruntergeladen (ca. 300 MB, mit deutschen Titeln ca. 750 MB) und lokal ausgewertet – ohne API-Schlüssel, ohne Anfragen je Film. Ohne Internetzugang die Dateien <code>title.basics.tsv.gz</code>, <code>title.ratings.tsv.gz</code> und optional <code>title.akas.tsv.gz</code> von datasets.imdbws.com nach <code>{status.data_dir}</code> kopieren und „Lokale Dateien importieren“ wählen.</p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-primary" disabled={busy} onClick={() => update.mutate(true)}><Download className="h-4 w-4" />{status.available ? "IMDb-Daten aktualisieren" : "IMDb-Daten herunterladen"}</button>
          <button className="btn-secondary" disabled={busy || !status.files.basics} onClick={() => update.mutate(false)}>Lokale Dateien importieren</button>
          <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={akas} onChange={(e) => setAkas(e.target.checked)} />deutsche Titel (empfohlen für deutsche Dateinamen)</label>
        </div>
        {st.running && (
          <div className="space-y-1 text-xs text-slate-500">
            <div>{st.phase} {st.bytes_total ? `– ${fmtBytes(st.bytes_done)} von ${fmtBytes(st.bytes_total)}` : st.rows ? `– ${fmtInt(st.rows)} Zeilen` : ""}</div>
            {st.bytes_total ? <ProgressBar value={st.bytes_done / st.bytes_total} /> : <ProgressBar value={0.5} striped />}
            <button className="btn-ghost text-xs" onClick={() => post("/api/movies/imdb/cancel")}>Abbrechen</button>
          </div>
        )}
        {status.match.running && <div className="text-xs text-slate-500">Zuordnung: {fmtInt(status.match.done)} / {fmtInt(status.match.total)}</div>}
        {!st.running && st.error && <ErrorBox error={`Aktualisierung fehlgeschlagen: ${st.error}`} />}
        {!busy && st.message && <p className="text-xs text-slate-500">{st.message}</p>}
        <p className="text-[11px] text-slate-500">{status.attribution} Nur für private, nicht-kommerzielle Nutzung.</p>
      </div>
    </Card>
  );
}

function MatchModal({ m, onClose }: { m: Movie; onClose: () => void }) {
  const [q, setQ] = useState(m.parsed_title ?? m.title);
  const [year, setYear] = useState<string>(m.year ? String(m.year) : "");
  const [search, setSearch] = useState({ q, year });
  const qc = useQueryClient();
  const toast = useToast();
  const cands = useQuery({ queryKey: ["candidates", m.file_id, search], queryFn: () => api<{ items: Candidate[] }>(`/api/movies/${m.file_id}/candidates${qs({ q: search.q, year: search.year || undefined })}`) });
  const done = () => { qc.invalidateQueries({ queryKey: ["movies"] }); onClose(); };
  const assign = useMutation({ mutationFn: (id: string | null) => put(`/api/movies/${m.file_id}/imdb`, { imdb_id: id }), onSuccess: () => { toast("Zuordnung gespeichert", "ok"); done(); }, onError: (e: Error) => toast(e.message, "error") });
  const auto = useMutation({ mutationFn: () => del(`/api/movies/${m.file_id}/imdb`), onSuccess: done });
  return (
    <Modal open wide title={`IMDb-Zuordnung: ${m.filename}`} onClose={onClose}
      footer={<><button className="btn-ghost" onClick={() => auto.mutate()}>Automatisch zuordnen</button><button className="btn-secondary" onClick={() => assign.mutate(null)}>Keine Zuordnung</button><button className="btn-secondary" onClick={onClose}>Schließen</button></>}>
      <form className="mb-3 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); setSearch({ q, year }); }}>
        <input className="input flex-1" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Titel oder IMDb-ID (tt…)" />
        <input className="input w-24" value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, ""))} placeholder="Jahr" />
        <button className="btn-primary"><Search className="h-4 w-4" />Suchen</button>
      </form>
      <ErrorBox error={cands.error} />
      {cands.data?.items.length === 0 && <Empty>Kein Treffer. Anderen Titel (z. B. Originaltitel) oder die IMDb-ID eingeben.</Empty>}
      <ul className="divide-y divide-slate-100 dark:divide-slate-800">
        {cands.data?.items.map((c) => (
          <li key={c.imdb_id} className="flex items-center justify-between gap-3 py-2">
            <div className="min-w-0">
              <div className="font-medium">{c.title_de ?? c.title} {c.year && <span className="text-slate-500">({c.year})</span>}</div>
              <div className="text-xs text-slate-500">{c.original_title !== (c.title_de ?? c.title) && `${c.original_title} · `}{c.type} · {c.runtime ? `${c.runtime} min · ` : ""}{c.genres} · <a className="hover:text-sky-500" href={`https://www.imdb.com/title/${c.imdb_id}/`} target="_blank" rel="noreferrer">{c.imdb_id}</a></div>
            </div>
            <div className="flex shrink-0 items-center gap-3">
              {c.rating != null && <span className="text-sm font-semibold tabular-nums">★ {c.rating.toLocaleString("de-DE")} <span className="text-xs font-normal text-slate-500">({fmtInt(c.votes)})</span></span>}
              <button className="btn-primary" onClick={() => assign.mutate(c.imdb_id)} disabled={m.imdb_id === c.imdb_id}>{m.imdb_id === c.imdb_id ? "zugeordnet" : "Zuordnen"}</button>
            </div>
          </li>
        ))}
      </ul>
    </Modal>
  );
}

function Detail({ m, onMatch }: { m: Movie; onMatch: () => void }) {
  return (
    <div className="grid gap-4 bg-slate-50 p-4 text-sm dark:bg-slate-900/60 lg:grid-cols-3">
      <div className="space-y-1">
        <div className="text-xs text-slate-500">Datei</div>
        <div className="[overflow-wrap:anywhere]">{m.path}</div>
        <div className="text-xs text-slate-500">{m.container} · {fmtBytes(m.size)}{m.bit_rate ? ` · ${fmtInt(Math.round(m.bit_rate / 1000))} kbit/s` : ""} · Laufzeit Datei {fmtDuration(m.duration)}{m.runtime_min ? ` · IMDb ${m.runtime_min} min` : ""}</div>
        <div className="flex flex-wrap gap-2 pt-1">
          <Link to={`/dateien/${m.file_id}`} className="btn-secondary">Integritätsprüfung</Link>
          {m.imdb_url && <a href={m.imdb_url} target="_blank" rel="noreferrer" className="btn-secondary"><ExternalLink className="h-4 w-4" />IMDb</a>}
          <button className="btn-secondary" onClick={onMatch}>Zuordnung ändern</button>
        </div>
        <div className="pt-1 text-xs text-slate-500">
          Zuordnung: {m.match_source ? SOURCE[m.match_source] ?? m.match_source : "–"}{m.match_confidence ? ` (${CONF[m.match_confidence] ?? m.match_confidence})` : ""}
          {m.parsed_title && <> · erkannt: „{m.parsed_title}“{m.year ? ` ${m.year}` : ""}</>}
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs text-slate-500">Video</div>
        {m.video ? <div>{CODEC[m.video.codec ?? ""] ?? m.video.codec} {m.video.profile && <span className="text-slate-500">({m.video.profile})</span>} · {m.video.width}×{m.video.height}{m.video.fps ? ` · ${m.video.fps.toLocaleString("de-DE", { maximumFractionDigits: 3 })} fps` : ""}{m.video.bit_depth ? ` · ${m.video.bit_depth} bit` : ""}{m.video.pix_fmt ? ` · ${m.video.pix_fmt}` : ""}</div> : <div className="text-slate-500">–</div>}
        <div className="mb-1 mt-3 text-xs text-slate-500">Untertitel</div>
        <ul>{m.subtitles.map((s) => <li key={s.index}>#{s.index} {s.language.toUpperCase()} {CODEC[s.codec ?? ""] ?? s.codec}{s.forced ? " (erzwungen)" : ""}{s.default ? " ★" : ""}{s.title ? <span className="text-slate-500"> – {s.title}</span> : null}</li>)}</ul>
        {m.subtitles.length === 0 && <div className="text-slate-500">keine</div>}
      </div>
      <div>
        <div className="mb-1 text-xs text-slate-500">Tonspuren</div>
        <ul className="space-y-0.5">
          {m.audio.map((a) => (
            <li key={a.index}>#{a.index} <b>{a.language_label}</b> {audioLabel(a).split(" ").slice(1).join(" ")}{a.channel_layout ? <span className="text-slate-500"> ({a.channel_layout})</span> : null}{a.bit_rate ? <span className="text-slate-500"> · {fmtInt(Math.round(a.bit_rate / 1000))} kbit/s</span> : null}{a.default ? " ★" : ""}{a.title ? <span className="text-slate-500"> – {a.title}</span> : null}</li>
          ))}
        </ul>
        {m.audio.length === 0 && <div className="text-slate-500">{m.probed ? "keine" : "noch nicht eingelesen"}</div>}
      </div>
    </div>
  );
}

export function Movies() {
  const [sp, setSp] = useSearchParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState(sp.get("q") ?? "");
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [match, setMatch] = useState<Movie | null>(null);
  const sort = sp.get("sort") ?? "title";
  const order = sp.get("order") ?? (sort === "rating" ? "desc" : "asc");
  const page = Number(sp.get("page") ?? 1);
  const pageSize = Number(sp.get("page_size") ?? 50);
  const filters = useMemo(() => ({
    q: sp.get("q") ?? undefined, genre: sp.get("genre") ?? undefined, audio_lang: sp.get("audio_lang") ?? undefined,
    sub_lang: sp.get("sub_lang") ?? undefined, min_rating: sp.get("min_rating") ?? undefined, resolution: sp.get("resolution") ?? undefined,
    matched: sp.get("matched") ?? undefined, library_id: sp.get("library_id") ?? undefined,
  }), [sp]);
  const set = (changes: Record<string, string | null>, resetPage = true) => {
    const n = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(changes)) { if (v === null || v === "") n.delete(k); else n.set(k, v); }
    if (resetPage) n.delete("page");
    setSp(n, { replace: true });
  };
  useEffect(() => { const t = setTimeout(() => { if ((sp.get("q") ?? "") !== search) set({ q: search || null }); }, 300); return () => clearTimeout(t); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const imdb = useQuery({ queryKey: ["imdb-status"], queryFn: () => api<ImdbStatus>("/api/movies/imdb"), refetchInterval: (q) => (q.state.data?.state.running || q.state.data?.match.running ? 1500 : 30000) });
  const busy = imdb.data?.state.running || imdb.data?.match.running;
  useEffect(() => { if (!busy) { qc.invalidateQueries({ queryKey: ["movies"] }); qc.invalidateQueries({ queryKey: ["movie-facets"] }); } }, [busy, qc]);
  const list = useQuery({ queryKey: ["movies", filters, sort, order, page, pageSize], queryFn: () => api<MovieList>(`/api/movies${qs({ ...filters, sort, order, page, page_size: pageSize })}`), placeholderData: keepPreviousData, refetchInterval: busy ? 3000 : false });
  const facets = useQuery({ queryKey: ["movie-facets"], queryFn: () => api<Facets>("/api/movies/facets") });
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api<{ id: number; name: string }[]>("/api/libraries"), staleTime: 60000 });
  const probe = useMutation({ mutationFn: () => post<{ id: number }>("/api/jobs", { task: "incremental", methods: ["quick"] }), onSuccess: (j) => toast(`Schnellprüfung #${j.id} gestartet – Tonspuren und Metadaten werden eingelesen`, "ok"), onError: (e: Error) => toast(e.message, "error") });
  const rematch = useMutation({ mutationFn: () => post("/api/movies/match"), onSuccess: () => qc.invalidateQueries({ queryKey: ["imdb-status"] }), onError: (e: Error) => toast(e.message, "error") });

  const setSort = (s: string) => set({ sort: s, order: sort === s ? (order === "asc" ? "desc" : "asc") : (["rating", "votes", "size", "added", "year", "resolution", "audio"].includes(s) ? "desc" : "asc") }, false);
  const toggle = (id: number) => { const n = new Set(open); if (n.has(id)) n.delete(id); else n.add(id); setOpen(n); };
  const items = list.data?.items ?? [];
  const f = facets.data;
  const exportUrl = `/api/movies/export.csv${qs({ ...filters, sort, order })}`;
  const active = Object.values(filters).filter(Boolean).length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Filme</h1>
          <p className="text-sm text-slate-500">{fmtInt(f?.total)} Filme · {fmtInt(f?.matched)} mit IMDb-Zuordnung{f?.average_rating ? ` · Ø Bewertung ${f.average_rating.toLocaleString("de-DE")}` : ""}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-secondary" disabled={!!busy} onClick={() => rematch.mutate()}><RefreshCw className="h-4 w-4" />Zuordnung aktualisieren</button>
          <a className="btn-secondary" href={exportUrl}><Download className="h-4 w-4" />CSV</a>
        </div>
      </div>

      {(!imdb.data?.available || busy || imdb.data?.state.error) && <ImdbPanel status={imdb.data} onChanged={() => qc.invalidateQueries({ queryKey: ["imdb-status"] })} />}
      {f && f.unprobed > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-400/40 bg-amber-400/10 px-4 py-2 text-sm">
          <span>{fmtInt(f.unprobed)} Datei(en) noch ohne Metadaten/Tonspuren.</span>
          <button className="btn-secondary" onClick={() => probe.mutate()} disabled={probe.isPending}>Metadaten einlesen (Schnellprüfung)</button>
        </div>
      )}

      <Card>
        <div className="flex flex-wrap gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-slate-400" />
            <input className="input pl-8" placeholder="Titel, Originaltitel oder Dateiname …" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <select className="input w-auto" value={filters.genre ?? ""} onChange={(e) => set({ genre: e.target.value })}>
            <option value="">Alle Genres</option>
            {f?.genres.map((g) => <option key={g.value} value={g.value}>{g.label} ({fmtInt(g.count)})</option>)}
          </select>
          <select className="input w-auto" value={filters.min_rating ?? ""} onChange={(e) => set({ min_rating: e.target.value })}>
            <option value="">Jede Bewertung</option>
            {[9, 8.5, 8, 7.5, 7, 6, 5].map((r) => <option key={r} value={r}>ab {r.toLocaleString("de-DE")} ★</option>)}
          </select>
          <select className="input w-auto" value={filters.audio_lang ?? ""} onChange={(e) => set({ audio_lang: e.target.value })}>
            <option value="">Alle Tonspur-Sprachen</option>
            {f?.audio_languages.map((g) => <option key={g.value} value={g.value}>Ton: {g.label} ({fmtInt(g.count)})</option>)}
          </select>
          <select className="input w-auto" value={filters.sub_lang ?? ""} onChange={(e) => set({ sub_lang: e.target.value })}>
            <option value="">Alle Untertitel</option>
            {f?.subtitle_languages.map((g) => <option key={g.value} value={g.value}>UT: {g.label} ({fmtInt(g.count)})</option>)}
          </select>
          <select className="input w-auto" value={filters.resolution ?? ""} onChange={(e) => set({ resolution: e.target.value })}>
            <option value="">Jede Auflösung</option><option value="2160">4K</option><option value="1080">1080p</option><option value="720">720p</option><option value="sd">SD</option>
          </select>
          <select className="input w-auto" value={filters.matched ?? ""} onChange={(e) => set({ matched: e.target.value })}>
            <option value="">Alle</option><option value="yes">mit IMDb-Zuordnung</option><option value="no">ohne IMDb-Zuordnung</option>
          </select>
          {(libs.data?.length ?? 0) > 1 && (
            <select className="input w-auto" value={filters.library_id ?? ""} onChange={(e) => set({ library_id: e.target.value })}>
              <option value="">Alle Bibliotheken</option>{libs.data?.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          {active > 0 && <button className="btn-ghost text-xs" onClick={() => { setSearch(""); setSp(new URLSearchParams(sort !== "title" ? { sort, order } : {}), { replace: true }); }}>Filter zurücksetzen</button>}
        </div>
      </Card>

      <div className="card overflow-hidden">
        <ErrorBox error={list.error} />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1200px]">
            <thead className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} className={`th ${c.cls ?? ""}`}>
                    {c.sort ? (
                      <button className={`inline-flex items-center gap-1 uppercase hover:text-sky-500 ${sort === c.sort ? "text-sky-600 dark:text-sky-300" : ""}`} onClick={() => setSort(c.sort!)}>
                        {c.label}{sort === c.sort && (order === "asc" ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />)}
                      </button>
                    ) : c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((m) => (
                <Fragment key={m.file_id}>
                  <tr className={`cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/40 ${list.isPlaceholderData ? "opacity-60" : ""}`} onClick={() => toggle(m.file_id)}>
                    <td className="td w-6 text-slate-400">{open.has(m.file_id) ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}</td>
                    <td className="td min-w-[220px] max-w-[340px]">
                      <div className="font-medium [overflow-wrap:anywhere]">{m.title}</div>
                      {m.original_title && <div className="text-[11px] text-slate-500 [overflow-wrap:anywhere]">{m.original_title}</div>}
                      {!m.imdb_id && m.match_confidence === "ambiguous" && <div className="text-[11px] text-amber-500">mehrdeutig – bitte zuordnen</div>}
                    </td>
                    <td className="td tabular-nums">{m.year ?? "–"}</td>
                    <td className="td"><Rating m={m} /></td>
                    <td className="td text-right tabular-nums text-xs">{runtime(m)}</td>
                    <td className="td max-w-[180px] text-xs">{m.genres.map((g) => g.label).join(", ") || "–"}</td>
                    <td className="td whitespace-nowrap text-xs">{m.video ? <><b>{resLabel(m.video)}</b> {CODEC[m.video.codec ?? ""] ?? m.video.codec}{(m.video.bit_depth ?? 8) >= 10 && <span className="ml-1 rounded bg-violet-500/15 px-1 text-[10px] text-violet-500">10 bit</span>}</> : "–"}</td>
                    <td className="td">
                      <div className="flex max-w-[300px] flex-wrap gap-1">
                        {m.audio.map((a) => <span key={a.index} title={a.title ?? undefined} className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${a.language === "ger" ? "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-400" : "bg-slate-500/10 text-slate-600 ring-slate-500/20 dark:text-slate-300"}`}>{audioLabel(a)}</span>)}
                        {m.audio.length === 0 && <span className="text-xs text-slate-400">{m.probed ? "–" : "nicht eingelesen"}</span>}
                      </div>
                    </td>
                    <td className="td text-xs">{[...new Set(m.subtitles.map((s) => s.language.toUpperCase()))].join(", ") || "–"}</td>
                    <td className="td whitespace-nowrap text-right text-xs tabular-nums">{fmtBytes(m.size)}</td>
                    <td className="td"><StatusBadge status={m.status} small /></td>
                    <td className="td whitespace-nowrap text-xs">{fmtDate(m.added, true)}</td>
                  </tr>
                  {open.has(m.file_id) && <tr><td colSpan={COLUMNS.length} className="p-0"><Detail m={m} onMatch={() => setMatch(m)} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {list.isLoading && <Loading />}
        {!list.isLoading && items.length === 0 && <Empty>Keine Filme gefunden.</Empty>}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          <select className="input w-auto" value={pageSize} onChange={(e) => set({ page_size: e.target.value })}>
            {[25, 50, 100, 250, 500].map((n) => <option key={n} value={n}>{n} pro Seite</option>)}
          </select>
          <Pagination page={page} pageSize={pageSize} total={list.data?.total ?? 0} onPage={(p) => set({ page: String(p) }, false)} />
        </div>
      </div>
      {imdb.data?.available && !busy && !imdb.data.state.error && (
        <p className="text-[11px] text-slate-500">IMDb-Daten vom {imdb.data.meta.dataset_date} · {imdb.data.attribution} · <button className="underline" onClick={() => post("/api/movies/imdb/update", { download: true, include_akas: true }).then(() => qc.invalidateQueries({ queryKey: ["imdb-status"] })).catch((e) => toast(String(e.message ?? e), "error"))}>jetzt aktualisieren</button></p>
      )}
      {match && <MatchModal m={match} onClose={() => setMatch(null)} />}
    </div>
  );
}
