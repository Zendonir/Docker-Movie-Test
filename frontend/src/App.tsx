import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { useLiveEvents } from "./useEvents";
import { Dashboard } from "./pages/Dashboard";
import { Files } from "./pages/Files";
import { FileDetailPage } from "./pages/FileDetail";
import { Scans } from "./pages/Scans";
import { JobDetailPage } from "./pages/JobDetail";
import { Schedules } from "./pages/Schedules";
import { Reports } from "./pages/Reports";
import { Libraries } from "./pages/Libraries";
import { SettingsPage } from "./pages/Settings";
import { Login } from "./pages/Login";
import { OptimizerPage } from "./pages/optimizer/Optimizer";

interface AuthStatus { auth_enabled: boolean; authenticated: boolean; username: string | null }

export function App() {
  const qc = useQueryClient();
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api<AuthStatus>("/api/auth/status"), staleTime: 60000 });
  useEffect(() => {
    const h = () => qc.invalidateQueries({ queryKey: ["auth"] });
    window.addEventListener("mis:unauthorized", h);
    return () => window.removeEventListener("mis:unauthorized", h);
  }, [qc]);
  const ok = !!auth.data?.authenticated;
  useLiveEvents(ok);
  if (auth.isLoading) return <Loading text="Verbinde …" />;
  if (!ok) return <Login />;
  return (
    <Layout authEnabled={!!auth.data?.auth_enabled}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/dateien" element={<Files />} />
        <Route path="/dateien/:id" element={<FileDetailPage />} />
        <Route path="/scans" element={<Scans />} />
        <Route path="/scans/:id" element={<JobDetailPage />} />
        <Route path="/zeitplaene" element={<Schedules />} />
        <Route path="/berichte" element={<Reports />} />
        <Route path="/bibliotheken" element={<Libraries />} />
        <Route path="/einstellungen" element={<SettingsPage />} />
        <Route path="/optimizer" element={<OptimizerPage />} />
        <Route path="/optimizer/:tab" element={<OptimizerPage />} />
        <Route path="*" element={<div className="p-8 text-slate-500">Seite nicht gefunden.</div>} />
      </Routes>
    </Layout>
  );
}
