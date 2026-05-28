import { useState } from "react";
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
import { fetchFundingHistory, tsMsToDate } from "../lib/api";
import { formatNumber, formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

// Default coins to show (can be expanded via strategy params in the future)
const DEFAULT_COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"];

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
        <ResponsiveContainer width="100%" height={120}>
          <LineChart
            data={chronological.map((r) => ({ ts_ms: r.ts_ms, rate_apr: r.annualized_pct }))}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis
              dataKey="ts_ms"
              tickFormatter={(v: number) =>
                tsMsToDate(v).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              }
              tick={{ fontSize: 10 }}
              minTickGap={60}
            />
            <YAxis
              domain={["auto", "auto"]}
              tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              tick={{ fontSize: 10 }}
              width={40}
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
  const [coins] = useState<string[]>(DEFAULT_COINS);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="funding" />
      <main className="mx-auto max-w-7xl p-4 space-y-4">
        <h1 className="text-lg font-semibold text-gray-800">Funding Rates</h1>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {coins.map((coin) => (
            <CoinFundingCard key={coin} coin={coin} />
          ))}
        </div>
      </main>
    </div>
  );
}
