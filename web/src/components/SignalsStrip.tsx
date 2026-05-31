import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategy,
  fetchFundingHistory,
  fetchFarbPositions,
  forceHourTick,
  manualOpenFarbPosition,
} from "../lib/api";

import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Skeleton } from "./ui/Skeleton";

const HOURS_PER_YEAR = 8760;

function SignalCard({ coin, entryThreshold, exitThreshold, window, hasPosition }: {
  coin: string;
  entryThreshold: number;
  exitThreshold: number;
  window: number;
  hasPosition: boolean;
}) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["funding-recent", coin, window],
    queryFn: () => fetchFundingHistory(coin, { limit: window }),
    refetchInterval: 60_000,
  });

  const openMutation = useMutation({
    mutationFn: () => manualOpenFarbPosition(coin),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["farb-positions-open"] });
      queryClient.invalidateQueries({ queryKey: ["farb-positions-active"] });
      queryClient.invalidateQueries({ queryKey: ["events"] });
    },
    onError: (err: Error) => {
      alert(err.message);
    },
  });

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
        <Skeleton rows={2} />
      </div>
    );
  }
  if (error instanceof Error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
        <p className="text-sm font-medium text-gray-700">{coin}</p>
        <p className="mt-1 text-xs text-red-500">err: {error.message}</p>
      </div>
    );
  }

  const rates = (data ?? []).map((r) => r.rate);
  const enoughData = rates.length >= window;
  const meanRate = rates.length > 0 ? rates.reduce((a, b) => a + b, 0) / rates.length : 0;
  const smoothedApr = meanRate * HOURS_PER_YEAR;
  const latestApr = data && data.length > 0 ? data[0].annualized_pct : null; // newest-first

  const status: "above_entry" | "neutral" | "below_exit" =
    !enoughData ? "neutral"
      : smoothedApr > entryThreshold * 100 ? "above_entry"
      : smoothedApr < exitThreshold * 100 ? "below_exit"
      : "neutral";

  const aprColor =
    status === "above_entry" ? "text-green-600"
    : status === "below_exit" ? "text-red-500"
    : enoughData ? "text-gray-700" : "text-gray-400";

  const statusLabel =
    !enoughData ? `need ${window - rates.length}h more data`
    : status === "above_entry" ? `entry (>${(entryThreshold * 100).toFixed(0)}%)`
    : status === "below_exit" ? `exit (<${(exitThreshold * 100).toFixed(0)}%)`
    : "neutral";

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-semibold text-gray-800">{coin}</span>
        <span className="text-[10px] uppercase tracking-wide text-gray-400">{statusLabel}</span>
      </div>
      <div className={`mt-1 text-xl font-semibold ${aprColor}`}>
        {enoughData ? `${smoothedApr.toFixed(2)}%` : "—"}
      </div>
      <div className="text-[11px] text-gray-500">
        smoothed {window}h · last hr {latestApr != null ? `${latestApr.toFixed(2)}%` : "—"}
      </div>
      {!hasPosition && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => {
              if (globalThis.confirm(`Открыть ${coin}?`)) {
                openMutation.mutate();
              }
            }}
            disabled={openMutation.isPending}
            className="rounded border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700 hover:bg-emerald-100 disabled:opacity-50"
          >
            {openMutation.isPending ? "Opening…" : "Open"}
          </button>
        </div>
      )}
    </div>
  );
}

export function SignalsStrip() {
  const strategyId = useActiveStrategyId();
  const queryClient = useQueryClient();
  const stratQ = useQuery({
    queryKey: ["strategy", strategyId],
    queryFn: () => fetchStrategy(strategyId!),
    enabled: !!strategyId,
  });

  const params = stratQ.data?.params_json as Record<string, unknown> | undefined;
  const coins = (params?.coins as string[] | undefined) ?? ["BTC", "ETH", "SOL"];
  const entryThreshold = (params?.entry_threshold_apr as number | undefined) ?? 0.10;
  const exitThreshold = (params?.phase2_exit_threshold as number | undefined) ?? -0.10;
  const sigWindow = (params?.signal_window_hours as number | undefined) ?? 12;

  const activeQ = useQuery({
    queryKey: ["farb-positions-active", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!, "active"),
    enabled: !!strategyId,
    refetchInterval: 60_000,
  });
  const coinsWithPosition = new Set<string>(
    (activeQ.data ?? []).map((fp) => fp.coin),
  );

  const tickMutation = useMutation({
    mutationFn: () => forceHourTick(strategyId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["funding-recent"] });
      queryClient.invalidateQueries({ queryKey: ["farb-positions-active"] });
      queryClient.invalidateQueries({ queryKey: ["farb-positions-open"] });
      queryClient.invalidateQueries({ queryKey: ["events"] });
    },
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-gray-700">Signals</h2>
        <div className="flex items-baseline gap-3">
          <span className="text-xs text-gray-500">
            entry {(entryThreshold * 100).toFixed(0)}% · exit {(exitThreshold * 100).toFixed(0)}% · window {sigWindow}h
          </span>
          <button
            type="button"
            onClick={() => tickMutation.mutate()}
            disabled={!strategyId || tickMutation.isPending}
            className="rounded border border-indigo-300 bg-indigo-50 px-2 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
            title="Run hour-tick now (fetch funding + evaluate entries/exits) without waiting for the next hour boundary"
          >
            {tickMutation.isPending ? "Ticking…" : "Force tick"}
          </button>
        </div>
      </div>
      {tickMutation.isError && (
        <p className="mb-2 text-xs text-red-600">{(tickMutation.error as Error).message}</p>
      )}
      {tickMutation.isSuccess && (
        <p className="mb-2 text-xs text-green-600">Forced tick ok at {new Date(tickMutation.data.ts_ms).toLocaleTimeString()}</p>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
        {coins.map((coin) => (
          <SignalCard
            key={coin}
            coin={coin}
            entryThreshold={entryThreshold}
            exitThreshold={exitThreshold}
            window={sigWindow}
            hasPosition={coinsWithPosition.has(coin)}
          />
        ))}
      </div>
    </div>
  );
}
