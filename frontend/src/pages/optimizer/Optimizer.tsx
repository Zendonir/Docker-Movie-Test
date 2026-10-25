import { NavLink, useParams } from "react-router-dom";
import { AudioLines, ListChecks, Scissors, Settings } from "lucide-react";
import { OptimizeTab } from "./Optimize";
import { AudioTab } from "./Audio";
import { JobsTab } from "./Jobs";
import { OptSettingsTab } from "./OptSettings";

const TABS = [
  { key: "", label: "Spuren optimieren", icon: Scissors },
  { key: "audio", label: "Audiokonvertierung", icon: AudioLines },
  { key: "auftraege", label: "Aufträge", icon: ListChecks },
  { key: "einstellungen", label: "Einstellungen", icon: Settings },
];

export function OptimizerPage() {
  const { tab = "" } = useParams();
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">MKV-Optimizer</h1>
        <p className="text-sm text-slate-500">Nicht benötigte Ton- und Untertitelspuren automatisch entfernen und Tonspuren umwandeln (mkvmerge/FFmpeg).</p>
      </div>
      <nav className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
        {TABS.map(({ key, label, icon: Icon }) => (
          <NavLink key={key} to={key ? `/optimizer/${key}` : "/optimizer"} end
            className={({ isActive }) => `-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium ${isActive ? "border-sky-500 text-sky-600 dark:text-sky-300" : "border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"}`}>
            <Icon className="h-4 w-4" />{label}
          </NavLink>
        ))}
      </nav>
      {tab === "" && <OptimizeTab />}
      {tab === "audio" && <AudioTab />}
      {tab === "auftraege" && <JobsTab />}
      {tab === "einstellungen" && <OptSettingsTab />}
    </div>
  );
}
