import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart,
  Line,
  ReferenceLine,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { fetchFundingHistory, fetchStrategy, tsMsToDate } from "../lib/api";
import { formatNumber, formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

// Fallback coin list used while the strategy params query is loading or
// if no active strategy is configured.
const FALLBACK_COINS = ["BTC", "ETH", "SOL"];

function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="animate-pulse space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 rounded bg-gray-200" />
      ))}
    </div>
  );
}

function ErrorMsg({ message }: { message: string }) {
  return (
    <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
      {message}
    </p>
  );
}

// ── Per-coin funding chart ────────────────────────────────────────────────────

function CoinFundingCard({ coin }: { coin: string }) {
  const now = useNow();
  const { data, isLoading, error } = useQuery({
    queryKey: ["funding", coin],
    queryFn: () => fetchFundingHistory(coin, { limit: 200 }),
    staleTime: 60_000,
  });

  // API returns newest-first; reverse for chronological display
  const chronological = (data ?? []).slice().reverse();

  // Latest APR
  const latest = data && data.length > 0 ? data[0] : null;
  const latestApr = latest?.annualized_pct ?? null;
  const latestTs = latest?.ts_ms ?? null;

  const aprColor =
    latestApr == null
      ? "text-gray-400"
      : latestApr > 20
        ? "text-green-600"
        : latestApr > 5
          ? "text-emerald-600"
          : latestApr >= 0
            ? "text-gray-700"
            : "text-red-500";

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-gray-800">{coin}</h3>
        <div className="flex items-baseline gap-2 text-xs">
          {latestApr != null && (
            <span className={`font-semibold text-base ${aprColor}`}>
              {formatNumber(latestApr, 2)}% APR
            </span>
          )}
          {latestTs != null && (
            <span className="text-gray-400">{formatRelative(latestTs, now)}</span>
          )}
        </div>
      </div>

      {isLoading && <Skeleton rows={4} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && chronological.length === 0 && (
        <p className="text-xs text-gray-400">No data</p>
      )}
      {!isLoading && !error && chronological.length > 0 && (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart
            data={chronological.map((r) => ({ ts_ms: r.ts_ms, rate_apr: r.annualized_pct }))}
            margin={{ top: 5, right: 20, bottom: 5, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis
              dataKey="ts_ms"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(v: number) => {
                const d = tsMsToDate(v);
                const sameDay = d.toDateString() === new Date().toDateString();
                return sameDay
                  ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                  : d.toLocaleString([], {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    });
              }}
              tick={{ fontSize: 11 }}
              minTickGap={80}
            />
            <YAxis
              domain={["auto", "auto"]}
              tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              tick={{ fontSize: 11 }}
              width={50}
            />
            <Tooltip
              formatter={(v: number) => [`${v.toFixed(3)}%`, "APR"]}
              labelFormatter={(v: number) => tsMsToDate(v).toLocaleString()}
            />
            <ReferenceLine y={0} stroke="#9ca3af" strokeDasharray="3 3" />
            <Line
              type="monotone"
              dataKey="rate_apr"
              stroke="#6366f1"
              strokeWidth={1.5}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ── Funding page ──────────────────────────────────────────────────────────────

export default function Funding() {
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);
  const stratQ = useQuery({
    queryKey: ["strategy", strategyId],
    queryFn: () => fetchStrategy(strategyId!),
    enabled: !!strategyId,
  });
  const [coins, setCoins] = useState<string[]>(FALLBACK_COINS);
  useEffect(() => {
    const fromParams = stratQ.data?.params_json?.coins;
    if (Array.isArray(fromParams) && fromParams.length > 0) {
      setCoins(fromParams as string[]);
    }
  }, [stratQ.data]);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="funding" />
      <main className="mx-auto max-w-7xl p-4 space-y-4">
        <h1 className="text-lg font-semibold text-gray-800">Funding Rates</h1>
        <div className="space-y-4">
          {coins.map((coin) => (
            <CoinFundingCard key={coin} coin={coin} />
          ))}
        </div>
      </main>
    </div>
  );
}
