import type { DetectedError } from "../types";
import { fmtPosition, fmtTimestamp } from "../format";
import { SEVERITY_META } from "./ui";

/**
 * Zeitleiste über die gesamte Filmlänge. Nur Befunde mit ermittelter Zeitposition
 * werden eingezeichnet – Befunde ohne Position erscheinen ausdrücklich darunter.
 */
export function Timeline({ duration, errors, selected, onSelect }: {
  duration: number | null; errors: DetectedError[]; selected: number | null; onSelect: (id: number) => void;
}) {
  const visible = errors.filter((e) => e.severity !== "info" && e.severity !== "incomplete");
  const placed = visible.filter((e) => e.time_precision !== "unknown" && e.time_start != null);
  const unplaced = visible.filter((e) => e.time_precision === "unknown" || e.time_start == null);
  const maxT = Math.max(duration ?? 0, ...placed.map((e) => e.time_end ?? e.time_start ?? 0));
  if (!maxT) {
    return <p className="text-sm text-slate-500">Keine Laufzeit bekannt – die Zeitleiste kann nicht dargestellt werden.</p>;
  }
  const pct = (t: number) => `${Math.min(100, Math.max(0, (t / maxT) * 100))}%`;
  // Überlappende Markierungen auf eigene Zeilen verteilen, damit jede anklickbar bleibt
  const minWidth = 0.6; // Prozent
  const sorted = [...placed].sort((a, b) => (a.time_start ?? 0) - (b.time_start ?? 0));
  const laneEnds: number[] = [];
  const lanes = new Map<number, number>();
  for (const e of sorted) {
    const s = ((e.time_start ?? 0) / maxT) * 100;
    const en = Math.max(((e.time_end ?? e.time_start ?? 0) / maxT) * 100, s + minWidth);
    let lane = laneEnds.findIndex((end) => end <= s);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(en);
    } else laneEnds[lane] = en;
    lanes.set(e.id, lane);
  }
  const laneCount = Math.max(laneEnds.length, 1);
  const laneH = 22;
  return (
    <div>
      <div className="relative rounded-lg bg-slate-100 dark:bg-slate-800" style={{ height: laneCount * laneH + 12 }} role="list" aria-label="Fehlerzeitleiste">
        {duration && maxT > duration && (
          <div className="absolute inset-y-0 border-l border-dashed border-slate-400" style={{ left: pct(duration) }} title="Laufzeit laut Container" />
        )}
        {sorted.map((e) => {
          const color = SEVERITY_META[e.severity]?.color ?? "#94a3b8";
          const start = e.time_start ?? 0;
          const end = e.time_end ?? start;
          const isBand = e.time_precision !== "exact" || end - start > 0.0005;
          const width = isBand ? Math.max(((end - start) / maxT) * 100, minWidth) : 0;
          const sel = selected === e.id;
          const lane = lanes.get(e.id) ?? 0;
          const vert = { top: 6 + lane * laneH, height: laneH - 4 };
          const style: React.CSSProperties = isBand
            ? {
                ...vert, left: pct(start), width: `${width}%`, background: e.time_precision === "approx"
                  ? `repeating-linear-gradient(45deg, ${color}aa 0 4px, ${color}33 4px 8px)` : `${color}bb`,
              }
            : { ...vert, left: pct(start), width: 4, marginLeft: -2, background: color };
          return (
            <button key={e.id} role="listitem" onClick={() => onSelect(e.id)}
              title={`${e.error_type_label}: ${fmtPosition(e.time_start, e.time_end, e.time_precision)}`}
              className={`absolute rounded-sm transition hover:ring-2 hover:ring-white/70 ${sel ? "ring-2 ring-sky-400" : ""}`} style={style} />
          );
        })}
      </div>
      <div className="mt-1 flex justify-between text-[11px] tabular-nums text-slate-500">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => <span key={f}>{fmtTimestamp(maxT * f, false)}</span>)}
      </div>
      <div className="mt-2 flex flex-wrap gap-4 text-[11px] text-slate-500">
        <span className="flex items-center gap-1"><span className="inline-block h-3 w-1 bg-slate-400" />exakter Zeitpunkt</span>
        <span className="flex items-center gap-1"><span className="inline-block h-3 w-5 bg-slate-400/70" />Bereich (Containerzeitstempel)</span>
        <span className="flex items-center gap-1"><span className="inline-block h-3 w-5" style={{ background: "repeating-linear-gradient(45deg,#94a3b8aa 0 4px,#94a3b833 4px 8px)" }} />ungefähr (Decoderfortschritt)</span>
      </div>
      {unplaced.length > 0 && (
        <p className="mt-2 text-xs text-slate-500">
          {unplaced.length} Befund(e) ohne bestimmbare Zeitposition – nicht auf der Zeitleiste eingezeichnet.
        </p>
      )}
    </div>
  );
}
