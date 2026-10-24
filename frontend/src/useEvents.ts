import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { LiveState } from "./types";

/** Verbindet sich per Server-Sent Events und aktualisiert die Daten ohne Neuladen. */
export function useLiveEvents(enabled: boolean) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let pending = false;
    const invalidateSoon = () => {
      if (pending) return;
      pending = true;
      setTimeout(() => {
        pending = false;
        qc.invalidateQueries({ queryKey: ["stats"] });
        qc.invalidateQueries({ queryKey: ["files"] });
        qc.invalidateQueries({ queryKey: ["jobs"] });
        qc.invalidateQueries({ queryKey: ["history"] });
        qc.invalidateQueries({ queryKey: ["error-types"] });
      }, 800);
    };
    const connect = () => {
      es = new EventSource("/api/events");
      es.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { event: string; data: unknown };
          if (msg.event === "progress") {
            qc.setQueryData(["live"], msg.data as LiveState);
          } else if (msg.event === "file") {
            const id = (msg.data as { id: number }).id;
            qc.invalidateQueries({ queryKey: ["file", id] });
            invalidateSoon();
          } else if (msg.event === "job") {
            const id = (msg.data as { id: number }).id;
            qc.invalidateQueries({ queryKey: ["job", id] });
            qc.invalidateQueries({ queryKey: ["live"] });
            invalidateSoon();
          } else if (msg.event === "stats" || msg.event === "scanner") {
            invalidateSoon();
          }
        } catch {
          /* ignorieren */
        }
      };
      es.onerror = () => {
        es?.close();
        if (!closed) retry = setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      es?.close();
    };
  }, [enabled, qc]);
}
