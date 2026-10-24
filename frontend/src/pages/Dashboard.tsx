import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, CheckCircle2, CircleDashed, Database, HardDrive, ListChecks, OctagonX, ShieldCheck, XCircle } from "lucide-react";
import { api } from "../api";
import { fmtBytes, fmtInt, fmtPct } from "../format";
import type { FileStatus, LiveState, Stats } from "../types";
import { Card, Empty, ErrorBox, Loading, ProgressBar, STATUS_META, StatCard } from "../components/ui";
import { LiveJobCard } from "../components/LiveJobCard";

interface HistoryRow { date: string; ok: number; warning: number; error: number; critical: number; incomplete: number; bytes: number }
interface ErrType { error_type: string; label: string; critical: number; error: number; warning: number; files: number; total: number }

const ORDER: FileStatus[] = ["critical", "error", "warning", "incomplete", "ok", "unchecked"];

function ChartTooltipStyle() {
  return { contentStyle: { background: "#0f172a", border: "1px solid #334155", borderRadius: 8, fontSize: 12, color: "#e2e8f0" }, itemStyle: { color: "#e2e8f0" } };
}

export function Dashboard() {
  const nav = useNavigate();
  const stats = useQuery({ queryKey: ["stats"], queryFn: () => api<Stats>("/api/stats") });
  const history = useQuery({ queryKey: ["history", 30], queryFn: () => api<HistoryRow[]>("/api/stats/history?days=30") });
  const types = useQuery({ queryKey: ["error-types"], queryFn: () => api<ErrType[]>("/api/stats/error-types") });
  const live = useQuery({ queryKey: ["live"], queryFn: () => api<LiveState>("/api/scan/live") });

  if (stats.isLoading) return <Loading />;
  if (stats.error) return <ErrorBox error={stats.error} />;
  const s = stats.data!;
  const goto = (q: string) => nav(`/dateien?${q}`);
  const pie = ORDER.map((k) => ({ key: k, name: STATUS_META[k].label, value: s.status[k] ?? 0, color: STATUS_META[k].color })).filter((x) => x.value > 0);
  const checkedFrac = s.total_size ? s.fully_checked_bytes / s.total_size : 0;
  const tt = ChartTooltipStyle();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <p className="text-sm text-slate-500">Zustand der Filmsammlung – aktualisiert sich automatisch.</p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <StatCard label="Mediendateien" value={fmtInt(s.total_files)} sub={s.missing ? `${fmtInt(s.missing)} nicht mehr vorhanden` : "in allen Bibliotheken"} icon={Database} tone="sky" onClick={() => goto("")} />
        <StatCard label="Vollständig geprüft" value={fmtInt(s.fully_checked)} sub={`${fmtPct(s.total_files ? s.fully_checked / s.total_files : 0)} · komplette Decodierung`} icon={ShieldCheck} tone="emerald" onClick={() => goto("check_level=full&check_level=container_full")} />
        <StatCard label="Noch nicht geprüft" value={fmtInt(s.unchecked)} sub="neu oder geändert" icon={CircleDashed} onClick={() => goto("only_new=1")} />
        <StatCard label="Kritische Fehler" value={fmtInt(s.status.critical)} sub="Datei ersetzen" icon={OctagonX} tone="red" onClick={() => goto("status=critical")} />
        <StatCard label="Sonstige Fehler" value={fmtInt(s.status.error)} sub="beschädigte Daten" icon={XCircle} tone="orange" onClick={() => goto("status=error")} />
        <StatCard label="Warnungen" value={fmtInt(s.status.warning)} sub="auffällig, kein sicherer Defekt" icon={AlertTriangle} tone="amber" onClick={() => goto("status=warning")} />
        <StatCard label="Ohne erkannte Fehler" value={fmtInt(s.status.ok)} sub={`davon ${fmtInt(s.ok_full)} vollständig decodiert`} icon={CheckCircle2} tone="emerald" onClick={() => goto("status=ok")} />
        <StatCard label="Nicht vollständig geprüft" value={fmtInt(s.status.incomplete)} sub="Abbruch, Codec, Rechte …" icon={ListChecks} tone="violet" onClick={() => goto("status=incomplete")} />
        <StatCard label="Bibliotheksgröße" value={fmtBytes(s.total_size)} sub={`${fmtInt(s.total_files)} Dateien`} icon={HardDrive} tone="sky" />
        <StatCard label="Geprüftes Datenvolumen" value={fmtBytes(s.fully_checked_bytes)} sub={`vollständig · ${fmtBytes(s.checked_bytes)} mit beliebiger Methode`} icon={ShieldCheck} tone="emerald" />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Dateigesundheit">
          {pie.length === 0 ? <Empty>Noch keine Dateien erfasst.</Empty> : (
            <div className="h-64">
              <ResponsiveContainer>
                <PieChart>
                  <Pie data={pie} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%" paddingAngle={1} onClick={(d: { key?: string }) => d.key && goto(`status=${d.key}`)} cursor="pointer">
                    {pie.map((p) => <Cell key={p.key} fill={p.color} stroke="none" />)}
                  </Pie>
                  <Tooltip {...tt} formatter={(v: number) => fmtInt(v)} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card title="Scanfortschritt" className="xl:col-span-2">
          <div className="mb-4 space-y-1">
            <div className="flex justify-between text-xs text-slate-500">
              <span>Vollständig decodiert (Datenvolumen)</span>
              <span className="tabular-nums">{fmtBytes(s.fully_checked_bytes)} von {fmtBytes(s.total_size)} · {fmtPct(checkedFrac)}</span>
            </div>
            <ProgressBar value={checkedFrac} tone="emerald" />
          </div>
          {live.data?.jobs.length ? (
            <div className="space-y-3">{live.data.jobs.slice(0, 3).map((j) => <LiveJobCard key={j.id} job={j} compact />)}</div>
          ) : (
            <Empty>Kein Scan aktiv. {s.scanner_paused && "Der Scanner ist pausiert."}</Empty>
          )}
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Scanergebnisse der letzten 30 Tage (geprüfte Dateien pro Tag)">
          {history.data && history.data.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer>
                <AreaChart data={history.data} margin={{ left: -10, right: 10 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#33415555" />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={(d: string) => d.slice(5).split("-").reverse().join(".")} />
                  <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip {...tt} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  {(["ok", "warning", "error", "critical", "incomplete"] as FileStatus[]).map((k) => (
                    <Area key={k} type="monotone" dataKey={k} name={STATUS_META[k].label} stackId="1" stroke={STATUS_META[k].color} fill={STATUS_META[k].color} fillOpacity={0.35} />
                  ))}
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : <Empty>Noch keine Prüfungen im Zeitraum.</Empty>}
        </Card>
        <Card title="Verteilung der Fehlertypen (aktuelle Befunde)">
          {types.data && types.data.length > 0 ? (
            <div style={{ height: Math.max(220, types.data.length * 30) }}>
              <ResponsiveContainer>
                <BarChart data={types.data.slice(0, 15)} layout="vertical" margin={{ left: 20, right: 10 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#33415555" />
                  <XAxis type="number" tick={{ fontSize: 11 }} allowDecimals={false} />
                  <YAxis type="category" dataKey="label" width={210} tick={{ fontSize: 11 }} />
                  <Tooltip {...tt} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="critical" name="Kritisch" stackId="a" fill={STATUS_META.critical.color} />
                  <Bar dataKey="error" name="Fehler" stackId="a" fill={STATUS_META.error.color} />
                  <Bar dataKey="warning" name="Warnung" stackId="a" fill={STATUS_META.warning.color} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <Empty>Keine Fehler oder Warnungen erkannt.</Empty>}
        </Card>
      </div>

      <Card title="Speicherplatz">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {s.storage.map((l) => {
            const st = l.storage;
            const used = st && st.total ? st.used / st.total : 0;
            return (
              <div key={l.library_id} className="space-y-1">
                <div className="flex justify-between text-sm"><span className="font-medium">{l.name}</span><span className="text-xs text-slate-500">{l.path}</span></div>
                {st ? (
                  <>
                    <ProgressBar value={used} tone={used > 0.9 ? "amber" : "sky"} />
                    <div className="text-xs text-slate-500">{fmtBytes(st.used)} belegt · {fmtBytes(st.free)} frei · {fmtBytes(st.total)} gesamt</div>
                  </>
                ) : <div className="text-xs text-slate-500">Nicht ermittelbar</div>}
              </div>
            );
          })}
          {s.storage.length === 0 && <Empty>Keine Bibliothek eingerichtet.</Empty>}
        </div>
      </Card>
    </div>
  );
}
