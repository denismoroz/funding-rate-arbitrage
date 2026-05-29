import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchAlerts, type Alert } from "../lib/api";
import { formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";

export function AlertBanner() {
  const strategyId = useActiveStrategyId();
  const now = useNow();
  const [open, setOpen] = useState<boolean | null>(null);

  const { data, isError } = useQuery({
    queryKey: ["alerts", strategyId],
    queryFn: () => fetchAlerts({ strategyId: strategyId! }),
    enabled: strategyId != null,
    refetchInterval: 30_000,
  });

  if (isError || !data || data.length === 0) return null;

  const hasError = data.some((a: Alert) => a.severity === "ERROR");
  const isOpen = open !== null ? open : hasError;

  const containerCls = hasError
    ? "rounded border bg-red-50 border-red-300 text-red-800"
    : "rounded border bg-yellow-50 border-yellow-300 text-yellow-800";

  const severityBadge = (sev: Alert["severity"]) =>
    sev === "ERROR"
      ? "rounded px-1 py-0.5 text-xs font-semibold uppercase bg-red-200 text-red-800"
      : "rounded px-1 py-0.5 text-xs font-semibold uppercase bg-yellow-200 text-yellow-800";

  return (
    <div className={`${containerCls} p-3`}>
      <div className="flex items-center justify-between">
        <span className="font-semibold">
          &#x26A0; {data.length} alert{data.length !== 1 ? "s" : ""}
        </span>
        <button
          onClick={() => setOpen(!isOpen)}
          className="text-sm underline opacity-70 hover:opacity-100"
        >
          {isOpen ? "hide" : "show"}
        </button>
      </div>
      {isOpen && (
        <ul className="mt-2 space-y-1">
          {data.map((a: Alert, i: number) => (
            <li key={i} className="flex items-center gap-2 text-sm">
              <span className="shrink-0 text-xs opacity-60">
                {formatRelative(a.ts, now)}
              </span>
              <span className={`shrink-0 ${severityBadge(a.severity)}`}>
                {a.severity}
              </span>
              {a.coin && (
                <span className="shrink-0 rounded bg-white/60 px-1 font-mono text-xs">
                  {a.coin}
                </span>
              )}
              <span className="min-w-0 flex-1 truncate">{a.message}</span>
              {a.position_id != null && (
                <span className="shrink-0 text-xs opacity-50">
                  #{a.position_id}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
