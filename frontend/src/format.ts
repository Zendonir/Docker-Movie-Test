const nf = new Intl.NumberFormat("de-DE");
const nf1 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

export const fmtInt = (n: number | null | undefined) => (n == null ? "–" : nf.format(n));

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return i === 0 ? `${nf.format(v)} B` : `${nf2.format(v)} ${units[i]}`;
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec == null || !isFinite(sec)) return "–";
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

export function fmtTimestamp(sec: number | null | undefined, ms = true): string {
  if (sec == null) return "–";
  const s = Math.max(0, sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s - h * 3600 - m * 60;
  const rs = ms ? r.toFixed(3).padStart(6, "0") : String(Math.floor(r)).padStart(2, "0");
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${rs}`;
}

export function fmtEta(sec: number | null | undefined): string {
  if (sec == null || !isFinite(sec)) return "wird ermittelt …";
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  if (h >= 48) return `${nf1.format(sec / 86400)} Tage`;
  return `${h} h ${m} min`;
}

export function fmtSpeed(bps: number | null | undefined): string {
  if (bps == null) return "–";
  return `${fmtBytes(bps)}/s`;
}

const df = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeStyle: "short" });
const dfs = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });

export function fmtDate(iso: string | null | undefined, short = false): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "–";
  return (short ? dfs : df).format(d);
}

export function fmtPct(f: number, digits = 1): string {
  return `${(f * 100).toLocaleString("de-DE", { maximumFractionDigits: digits })} %`;
}

export function fmtPosition(start: number | null, end: number | null, precision: string): string {
  if (precision === "unknown" || start == null) return "Position nicht bestimmbar";
  const p = precision === "approx" ? "ca. " : "";
  if (end != null && Math.abs(end - start) > 0.0005) return `${p}${fmtTimestamp(start)} – ${fmtTimestamp(end)}`;
  return `${p}${fmtTimestamp(start)}`;
}

export const PRECISION_LABELS: Record<string, string> = {
  exact: "exakt (Container-/Paketzeitstempel)",
  range: "Bereich zwischen gemessenen Zeitstempeln",
  approx: "ungefähr (Decoderfortschritt, nicht framegenau)",
  unknown: "nicht bestimmbar",
};

export const METHOD_LABELS: Record<string, string> = {
  quick: "Schnellprüfung",
  container: "Containerprüfung",
  full: "Vollständige Prüfung",
};

export const JOB_STATE_LABELS: Record<string, string> = {
  queued: "Wartend",
  running: "Läuft",
  paused: "Pausiert",
  completed: "Abgeschlossen",
  cancelled: "Abgebrochen",
  failed: "Fehlgeschlagen",
  interrupted: "Unterbrochen",
};
