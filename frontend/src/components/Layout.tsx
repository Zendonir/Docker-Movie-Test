import { ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, FileBarChart2, FolderTree, Gauge, ListVideo, LogOut, Menu, Moon, Pause, Play, ScanLine, Settings, Sun } from "lucide-react";
import { api, post } from "../api";
import type { LiveState, Stats } from "../types";
import { useToast } from "./toast";

const NAV = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/dateien", label: "Dateien", icon: ListVideo },
  { to: "/scans", label: "Scans", icon: ScanLine },
  { to: "/zeitplaene", label: "Zeitpläne", icon: CalendarClock },
  { to: "/berichte", label: "Berichte", icon: FileBarChart2 },
  { to: "/bibliotheken", label: "Bibliotheken", icon: FolderTree },
  { to: "/einstellungen", label: "Einstellungen", icon: Settings },
];

export function Layout({ children, authEnabled }: { children: ReactNode; authEnabled: boolean }) {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  const qc = useQueryClient();
  const toast = useToast();
  useEffect(() => setOpen(false), [loc.pathname]);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("mis-theme", dark ? "dark" : "light");
    } catch {
      /* ignorieren */
    }
  }, [dark]);

  const stats = useQuery({ queryKey: ["stats"], queryFn: () => api<Stats>("/api/stats") });
  const live = useQuery({ queryKey: ["live"], queryFn: () => api<LiveState>("/api/scan/live"), refetchInterval: 15000 });
  const paused = stats.data?.scanner_paused ?? false;
  const toggle = useMutation({
    mutationFn: () => post(paused ? "/api/scanner/resume" : "/api/scanner/pause"),
    onSuccess: () => {
      toast(paused ? "Scanner fortgesetzt" : "Scanner pausiert", "ok");
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => toast(e.message, "error"),
  });
  const logout = useMutation({ mutationFn: () => post("/api/auth/logout"), onSuccess: () => window.location.reload() });
  const running = live.data?.running ?? 0;

  return (
    <div className="flex min-h-full">
      <aside className={`fixed inset-y-0 left-0 z-40 w-60 shrink-0 border-r border-slate-200 bg-white transition-transform dark:border-slate-800 dark:bg-slate-900 lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex h-14 items-center gap-2 border-b border-slate-200 px-4 dark:border-slate-800">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <div className="leading-tight">
            <div className="text-sm font-semibold">Media Integrity</div>
            <div className="text-[11px] text-slate-500">Scanner</div>
          </div>
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition ${isActive ? "bg-sky-500/15 text-sky-700 dark:text-sky-300" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"}`}>
              <Icon className="h-4 w-4" />
              {label}
              {to === "/scans" && running > 0 && (
                <span className="ml-auto rounded-full bg-sky-500 px-1.5 text-[10px] font-semibold text-white">{running}</span>
              )}
            </NavLink>
          ))}
        </nav>
      </aside>
      {open && <div className="fixed inset-0 z-30 bg-slate-950/50 lg:hidden" onClick={() => setOpen(false)} />}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-slate-200 bg-white/80 px-4 backdrop-blur dark:border-slate-800 dark:bg-slate-950/80">
          <button className="btn-ghost p-1.5 lg:hidden" onClick={() => setOpen(true)} aria-label="Menü"><Menu className="h-5 w-5" /></button>
          <div className="flex-1" />
          {paused && <span className="rounded-full bg-amber-400/20 px-2.5 py-1 text-xs font-medium text-amber-600 dark:text-amber-300">Scanner pausiert</span>}
          <button className="btn-secondary" onClick={() => toggle.mutate()} disabled={toggle.isPending} title="Alle Prüfungen anhalten bzw. fortsetzen">
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            <span className="hidden sm:inline">{paused ? "Scanner fortsetzen" : "Scanner pausieren"}</span>
          </button>
          <button className="btn-ghost p-1.5" onClick={() => setDark((d) => !d)} aria-label="Farbschema wechseln" title="Hell/Dunkel">
            {dark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </button>
          {authEnabled && (
            <button className="btn-ghost p-1.5" onClick={() => logout.mutate()} title="Abmelden"><LogOut className="h-5 w-5" /></button>
          )}
        </header>
        <main className="mx-auto w-full max-w-[1600px] flex-1 p-4 lg:p-6">{children}</main>
      </div>
    </div>
  );
}
