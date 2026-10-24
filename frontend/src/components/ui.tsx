import { ReactNode, useEffect } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, CircleHelp, Loader2, OctagonX, ShieldAlert, X, XCircle } from "lucide-react";
import type { FileStatus, Severity } from "../types";

export const STATUS_META: Record<FileStatus, { label: string; cls: string; dot: string; color: string; icon: typeof CheckCircle2 }> = {
  critical: { label: "Kritisch", cls: "bg-red-500/15 text-red-600 ring-red-500/30 dark:text-red-400", dot: "bg-red-500", color: "#ef4444", icon: OctagonX },
  error: { label: "Fehler", cls: "bg-orange-500/15 text-orange-600 ring-orange-500/30 dark:text-orange-400", dot: "bg-orange-500", color: "#f97316", icon: XCircle },
  warning: { label: "Warnung", cls: "bg-amber-400/15 text-amber-700 ring-amber-400/30 dark:text-amber-300", dot: "bg-amber-400", color: "#fbbf24", icon: AlertTriangle },
  ok: { label: "Ohne erkannte Fehler", cls: "bg-emerald-500/15 text-emerald-700 ring-emerald-500/30 dark:text-emerald-400", dot: "bg-emerald-500", color: "#10b981", icon: CheckCircle2 },
  incomplete: { label: "Nicht vollständig geprüft", cls: "bg-violet-500/15 text-violet-700 ring-violet-500/30 dark:text-violet-300", dot: "bg-violet-400", color: "#a78bfa", icon: CircleHelp },
  unchecked: { label: "Nicht geprüft", cls: "bg-slate-500/15 text-slate-600 ring-slate-500/30 dark:text-slate-400", dot: "bg-slate-400", color: "#64748b", icon: CircleDashed },
};

export const SEVERITY_META: Record<Severity, { label: string; cls: string; color: string }> = {
  critical: { label: "Kritisch", cls: STATUS_META.critical.cls, color: "#ef4444" },
  error: { label: "Fehler", cls: STATUS_META.error.cls, color: "#f97316" },
  warning: { label: "Warnung", cls: STATUS_META.warning.cls, color: "#fbbf24" },
  incomplete: { label: "Nicht geprüft", cls: STATUS_META.incomplete.cls, color: "#a78bfa" },
  info: { label: "Info", cls: "bg-sky-500/15 text-sky-700 ring-sky-500/30 dark:text-sky-300", color: "#38bdf8" },
};

export function StatusBadge({ status, label, small }: { status: FileStatus; label?: string; small?: boolean }) {
  const m = STATUS_META[status] ?? STATUS_META.unchecked;
  const Icon = m.icon;
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-medium ring-1 ring-inset ${m.cls} ${small ? "text-[11px]" : "text-xs"}`}>
      <Icon className="h-3.5 w-3.5" />
      {label ?? m.label}
    </span>
  );
}

export function SeverityBadge({ severity, heuristic }: { severity: Severity; heuristic?: boolean }) {
  const m = SEVERITY_META[severity] ?? SEVERITY_META.info;
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${m.cls}`}>{m.label}</span>
      {heuristic && (
        <span title="Heuristischer Hinweis – kein zweifelsfreier Defektnachweis" className="rounded bg-slate-500/15 px-1.5 py-0.5 text-[10px] font-medium text-slate-500 dark:text-slate-400">
          Hinweis
        </span>
      )}
    </span>
  );
}

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h2>
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function StatCard({ label, value, sub, icon: Icon, tone = "slate", onClick }: {
  label: string; value: ReactNode; sub?: ReactNode; icon?: typeof CheckCircle2; tone?: string; onClick?: () => void;
}) {
  const tones: Record<string, string> = {
    slate: "text-slate-500", red: "text-red-500", orange: "text-orange-500", amber: "text-amber-400",
    emerald: "text-emerald-500", sky: "text-sky-500", violet: "text-violet-400",
  };
  return (
    <button type="button" onClick={onClick} disabled={!onClick}
      className="card flex w-full flex-col items-start gap-1 p-4 text-left transition enabled:hover:border-sky-500/50 disabled:cursor-default">
      <div className="flex w-full items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</span>
        {Icon && <Icon className={`h-5 w-5 ${tones[tone] ?? tones.slate}`} />}
      </div>
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      {sub && <span className="text-xs text-slate-500 dark:text-slate-400">{sub}</span>}
    </button>
  );
}

export function ProgressBar({ value, tone = "sky", striped }: { value: number; tone?: "sky" | "emerald" | "amber"; striped?: boolean }) {
  const pct = Math.max(0, Math.min(100, value * 100));
  const colors = { sky: "bg-sky-500", emerald: "bg-emerald-500", amber: "bg-amber-400" };
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
      <div className={`h-full rounded-full ${colors[tone]} transition-[width] duration-700 ${striped ? "animate-pulse" : ""}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return <Loader2 className={`animate-spin ${className}`} />;
}

export function Loading({ text = "Lade …" }: { text?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-slate-500">
      <Spinner /> {text}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-300">
      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" /> {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-8 text-center text-sm text-slate-500 dark:text-slate-400">{children}</div>;
}

export function Pagination({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
      <span>
        {total === 0 ? "Keine Einträge" : `${((page - 1) * pageSize + 1).toLocaleString("de-DE")}–${Math.min(page * pageSize, total).toLocaleString("de-DE")} von ${total.toLocaleString("de-DE")}`}
      </span>
      <div className="flex items-center gap-1">
        <button className="btn-secondary" disabled={page <= 1} onClick={() => onPage(1)}>«</button>
        <button className="btn-secondary" disabled={page <= 1} onClick={() => onPage(page - 1)}>Zurück</button>
        <span className="px-2 tabular-nums">Seite {page} / {pages}</span>
        <button className="btn-secondary" disabled={page >= pages} onClick={() => onPage(page + 1)}>Weiter</button>
        <button className="btn-secondary" disabled={page >= pages} onClick={() => onPage(pages)}>»</button>
      </div>
    </div>
  );
}

export function Modal({ open, title, onClose, children, footer, wide }: {
  open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-950/60 p-4 pt-16 backdrop-blur-sm" onMouseDown={onClose}>
      <div className={`card w-full ${wide ? "max-w-4xl" : "max-w-lg"}`} onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={title}>
        <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <h3 className="font-semibold">{title}</h3>
          <button className="btn-ghost p-1" onClick={onClose} aria-label="Schließen"><X className="h-4 w-4" /></button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto p-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">{footer}</footer>}
      </div>
    </div>
  );
}

export function Toggle({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; hint?: ReactNode; disabled?: boolean }) {
  return (
    <label className={`flex items-start gap-3 ${disabled ? "opacity-50" : "cursor-pointer"}`}>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)}
        className={`relative mt-0.5 inline-flex h-5 w-9 shrink-0 rounded-full transition ${checked ? "bg-sky-600" : "bg-slate-300 dark:bg-slate-700"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition ${checked ? "left-4.5" : "left-0.5"}`} />
      </button>
      <span className="text-sm">
        <span className="font-medium">{label}</span>
        {hint && <span className="block text-xs text-slate-500 dark:text-slate-400">{hint}</span>}
      </span>
    </label>
  );
}

export function MethodChips({ methods }: { methods: string[] }) {
  const names: Record<string, string> = { quick: "Schnell", container: "Container", full: "Vollständig" };
  return (
    <span className="inline-flex flex-wrap gap-1">
      {methods.map((m) => (
        <span key={m} className="rounded bg-slate-500/15 px-1.5 py-0.5 text-[11px] font-medium text-slate-600 dark:text-slate-300">{names[m] ?? m}</span>
      ))}
    </span>
  );
}
