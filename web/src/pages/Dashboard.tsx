import { useState, useMemo } from "react";
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
import {
  fetchStrategies,
  fetchEquity,
  fetchPositions,
  fetchSignals,
  fetchEvents,
  fetchFundingHistory,
  fetchPositionFundingHistory,
  fetchAlerts,
  fetchWallet,
  fetchWalletHistory,
  type Alert,
  type Fill,
  type Position,
} from "../lib/api";
import { formatCurrency, formatCurrencyPrecise, formatQty, formatRelative, formatNumber } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useLiveEvents, type WsStatus } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";

// ── Skeleton ──────────────────────────────────────────────────────────────────

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

// ── Equity chart tooltip ───────────────────────────────────────────────────────

type ChartPoint = {
  ts: string;
  value: number;
  // Optional sidecars (only present when the row is a strategy snapshot)
  funding_cum?: number;
  fees_cum?: number;
};

function EquityTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: ChartPoint }>;
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded border border-gray-200 bg-white p-2 text-xs shadow">
      <p className="font-semibold">{formatCurrency(d.value)}</p>
      {d.funding_cum != null && (
        <p className="text-green-600">funding: {formatCurrencyPrecise(d.funding_cum)}</p>
      )}
      {d.fees_cum != null && (
        <p className="text-red-500">fees: {formatCurrencyPrecise(d.fees_cum)}</p>
      )}
      <p className="text-gray-400">{new Date(d.ts).toLocaleTimeString()}</p>
    </div>
  );
}

// ── WS status dot ─────────────────────────────────────────────────────────────

const WS_DOT: Record<WsStatus, string> = {
  open: "bg-green-500",
  connecting: "bg-yellow-500",
  closed: "bg-red-500",
};

// ── Header ────────────────────────────────────────────────────────────────────

function Header({ wsStatus, route }: { wsStatus: WsStatus; route: "dashboard" | "settings" }) {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const stratQ = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    staleTime: 60_000,
  });

  const eventsQ = useQuery({
    queryKey: ["events-header"],
    queryFn: () => fetchEvents({ limit: 20 }),
  });

  const strategy = stratQ.data?.find((s) => s.id === strategyId);

  // Engine liveness: a tick.completed within the last 90s means the engine
  // is alive regardless of whether the latest engine.* event happens to be
  // a stale 'engine.stopping' from a previous process shutdown.
  const lastTick = eventsQ.data?.find((e) => e.kind === "tick.completed");
  const lastTickAgeMs = lastTick ? now - new Date(lastTick.ts).getTime() : Infinity;
  const engineAlive = lastTickAgeMs <= 90_000;
  const engineEvent = engineAlive
    ? lastTick
    : eventsQ.data?.find((e) => e.kind.startsWith("engine."));
  const engineLabel = engineAlive ? "running" : engineEvent?.message;

  return (
    <header className="flex items-center gap-4 border-b border-gray-200 bg-gray-900 px-6 py-3">
      <span className="text-lg font-bold tracking-tight text-white">frab</span>

      <nav className="flex items-center gap-3 text-sm">
        <a
          href="#/"
          className={route === "dashboard" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Dashboard
        </a>
        <a
          href="#/settings"
          className={route === "settings" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Settings
        </a>
      </nav>

      {strategy && (
        <span className="rounded-full bg-indigo-700 px-2.5 py-0.5 text-xs font-medium text-white">
          {strategy.name} {strategy.version} · {strategy.status}
        </span>
      )}

      {engineEvent && (
        <span className="ml-auto flex items-center gap-1.5 text-xs text-gray-400">
          engine:{" "}
          <span className="text-gray-200">{engineLabel}</span>
          <span className="text-gray-500">
            ({formatRelative(engineEvent.ts, now)})
          </span>
          <span
            className={`inline-block h-2 w-2 rounded-full ${WS_DOT[wsStatus]}`}
            title={wsStatus}
          />
        </span>
      )}

      {!engineEvent && (
        <span className="ml-auto flex items-center gap-1.5 text-xs text-gray-400">
          <span
            className={`inline-block h-2 w-2 rounded-full ${WS_DOT[wsStatus]}`}
            title={wsStatus}
          />
        </span>
      )}
    </header>
  );
}

// ── Equity card ───────────────────────────────────────────────────────────────

function EquityCard() {
  const strategyId = useActiveStrategyId();
  const since24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();

  const { data: stratData, isLoading: stratLoading, error: stratError } = useQuery({
    queryKey: ["equity", strategyId],
    queryFn: () => fetchEquity(strategyId!, { limit: 2000, since: since24h }),
    enabled: !!strategyId,
  });

  const { data: walletHistory } = useQuery({
    queryKey: ["wallet-history"],
    queryFn: () => fetchWalletHistory({ limit: 2000, since: since24h }),
    refetchInterval: 30_000,
  });

  const { data: walletData } = useQuery({
    queryKey: ["wallet", strategyId],
    queryFn: () => fetchWallet(strategyId!),
    enabled: !!strategyId,
    refetchInterval: 30_000,
  });

  // Prefer wallet history (real HL state); fall back to strategy synthetic equity.
  const usingWallet = (walletHistory?.length ?? 0) > 0;
  const isLoading = stratLoading;
  const error = stratError;

  const slice: ChartPoint[] = useMemo(() => {
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    if (usingWallet) {
      const all = (walletHistory ?? []).map<ChartPoint>((w) => ({
        ts: w.ts,
        value: w.account_value,
      }));
      const recent = all.filter((d) => new Date(d.ts).getTime() >= cutoff);
      return recent.length > 0 ? recent : all;
    }
    const all = (stratData ?? []).map<ChartPoint>((s) => ({
      ts: s.ts,
      value: s.total_equity,
      funding_cum: s.funding_cum,
      fees_cum: s.fees_cum,
    }));
    const recent = all.filter((d) => new Date(d.ts).getTime() >= cutoff);
    return recent.length > 0 ? recent : all;
  }, [usingWallet, walletHistory, stratData]);

  const latestStrat = stratData && stratData.length > 0 ? stratData[stratData.length - 1] : undefined;

  // Use wallet live total when available; fall back to local accounting equity.
  const totalDisplay =
    walletData != null ? walletData.total_usd : latestStrat?.total_equity;

  const { yDecimals, yDomain, chartTitle } = useMemo(() => {
    const prefix = usingWallet ? "Wallet" : "Equity";
    if (slice.length === 0) {
      return {
        yDecimals: 2,
        yDomain: ["auto", "auto"] as [string, string],
        chartTitle: `${prefix} (last 24h)`,
      };
    }
    const values = slice.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const sp = max - min;

    let dec: number;
    if (sp >= 10) dec = 0;
    else if (sp >= 1) dec = 2;
    else if (sp >= 0.01) dec = 3;
    else dec = 4;

    const domain: [string, string] = sp < 1
      ? ["dataMin - 0.001", "dataMax + 0.001"]
      : ["auto", "auto"];

    const firstTs = new Date(slice[0].ts).getTime();
    const lastTs = new Date(slice[slice.length - 1].ts).getTime();
    const spanMs = lastTs - firstTs;
    const spanHours = spanMs / (1000 * 60 * 60);
    const spanMinutes = spanMs / (1000 * 60);
    let title: string;
    if (spanHours >= 23) {
      title = `${prefix} (last 24h)`;
    } else if (spanHours >= 1) {
      title = `${prefix} (last ${Math.floor(spanHours)}h)`;
    } else {
      const mins = Math.floor(spanMinutes / 5) * 5 || Math.floor(spanMinutes);
      title = `${prefix} (last ${mins}m)`;
    }

    return { yDecimals: dec, yDomain: domain, chartTitle: title };
  }, [slice, usingWallet]);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-gray-700">
          {chartTitle}
        </h2>
        {(totalDisplay != null || latestStrat) && (
          <div className="flex items-baseline gap-3 text-xs text-gray-500">
            <span>
              Total{" "}
              <span className="text-base font-semibold text-gray-900">
                {totalDisplay != null ? formatCurrency(totalDisplay) : "—"}
              </span>
              {walletData != null && (
                <span className="ml-1 text-xs text-gray-400">(wallet)</span>
              )}
            </span>
            {latestStrat && (
              <>
                <span className="text-green-600">
                  funding {formatCurrencyPrecise(latestStrat.funding_cum)}
                </span>
                <span className="text-red-500">
                  fees {formatCurrencyPrecise(latestStrat.fees_cum)}
                </span>
              </>
            )}
          </div>
        )}
      </div>
      {isLoading && <Skeleton rows={6} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={slice}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis
              dataKey="ts"
              tickFormatter={(v: string) =>
                new Date(v).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              }
              tick={{ fontSize: 11 }}
              minTickGap={60}
            />
            <YAxis
              domain={yDomain}
              tickFormatter={(v: number) => {
                if (Math.abs(v) >= 1000) return `$${(v / 1000).toFixed(2)}k`;
                return `$${v.toFixed(yDecimals)}`;
              }}
              tick={{ fontSize: 11 }}
              width={70}
            />
            <Tooltip content={<EquityTooltip />} />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#6366f1"
              strokeWidth={2}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ── Position details modal ────────────────────────────────────────────────────

function PositionDetailsModal({
  position,
  onClose,
}: {
  position: Position;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["funding", position.coin],
    queryFn: () => fetchFundingHistory(position.coin, { limit: 200 }),
  });

  const { data: accruals } = useQuery({
    queryKey: ["funding-accruals", position.id],
    queryFn: () => fetchPositionFundingHistory(position.id, { limit: 500 }),
  });

  // API returns newest-first; reverse for chronological chart
  const chronological = (data ?? []).slice().reverse();

  const realCumulative = (() => {
    const series = [{ ts: position.opened_at, cum_funding: 0 }];
    let acc = 0;
    for (const a of accruals ?? []) {
      acc += a.delta;
      series.push({ ts: a.ts, cum_funding: acc });
    }
    return series;
  })();
  const showDots = realCumulative.length < 10;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl rounded-lg bg-white p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-baseline justify-between">
          <div>
            <h3 className="text-lg font-semibold text-gray-900">
              {position.coin} · funding history
            </h3>
            <p className="text-xs text-gray-500">
              Opened {formatRelative(position.opened_at)} · Entry perp $
              {formatNumber(position.entry_perp_price, 4)} · Funding so far{" "}
              <span
                className={
                  position.funding_collected >= 0
                    ? "text-green-600"
                    : "text-red-500"
                }
              >
                {formatCurrency(position.funding_collected)}
              </span>
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {isLoading && <Skeleton rows={6} />}
        {error instanceof Error && <ErrorMsg message={error.message} />}

        {!isLoading && !error && chronological.length === 0 && (
          <p className="text-sm text-gray-400">No funding history yet.</p>
        )}

        {!isLoading && !error && chronological.length > 0 && (
          <>
            <h4 className="mb-1 text-xs font-medium text-gray-500">
              Funding rate (% APR)
            </h4>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={chronological.map((r) => ({ ts: r.ts, rate_apr: r.annualized_pct }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="ts"
                  tickFormatter={(v: string) =>
                    new Date(v).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  }
                  tick={{ fontSize: 11 }}
                  minTickGap={60}
                />
                <YAxis
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                  tick={{ fontSize: 11 }}
                  width={55}
                />
                <Tooltip
                  formatter={(v: number) => [`${v.toFixed(3)}%`, "APR"]}
                  labelFormatter={(v: string) =>
                    new Date(v).toLocaleString()
                  }
                />
                <ReferenceLine y={0} stroke="#9ca3af" strokeDasharray="3 3" />
                <Line
                  type="monotone"
                  dataKey="rate_apr"
                  stroke="#6366f1"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>

            <h4 className="mb-1 mt-3 text-xs font-medium text-gray-500">
              Cumulative funding for this position ($, since open)
            </h4>
            <ResponsiveContainer width="100%" height={140}>
              <LineChart data={realCumulative}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="ts"
                  tickFormatter={(v: string) =>
                    new Date(v).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  }
                  tick={{ fontSize: 11 }}
                  minTickGap={60}
                />
                <YAxis
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) =>
                    Math.abs(v) >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`
                  }
                  tick={{ fontSize: 11 }}
                  width={70}
                />
                <Tooltip
                  formatter={(v: number) => [formatCurrency(v), "cum funding"]}
                  labelFormatter={(v: string) =>
                    new Date(v).toLocaleString()
                  }
                />
                <ReferenceLine y={0} stroke="#9ca3af" strokeDasharray="3 3" />
                <Line
                  type="monotone"
                  dataKey="cum_funding"
                  stroke="#16a34a"
                  strokeWidth={2}
                  dot={showDots ? { r: 3, fill: "#16a34a" } : false}
                />
              </LineChart>
            </ResponsiveContainer>
          </>
        )}
      </div>
    </div>
  );
}

// ── Open positions helpers ─────────────────────────────────────────────────────

function formatRelativeFuture(iso: string | null, nowMs: number): string {
  if (!iso) return "—";
  const diffMs = new Date(iso).getTime() - nowMs;
  if (diffMs <= 0) return "passed";
  const days = Math.floor(diffMs / (24 * 3600 * 1000));
  const hours = Math.floor((diffMs % (24 * 3600 * 1000)) / (3600 * 1000));
  if (days >= 1) return `in ${days}d ${hours}h`;
  if (hours >= 1) return `in ${hours}h`;
  const mins = Math.floor(diffMs / (60 * 1000));
  return `in ${mins}m`;
}

function beColor(iso: string | null, nowMs: number): string {
  if (!iso) return "text-gray-500";
  const diffMs = new Date(iso).getTime() - nowMs;
  if (diffMs <= 0) return "text-gray-500";
  if (diffMs < 7 * 24 * 3600 * 1000) return "text-amber-600";
  return "text-gray-700";
}

// ── Open positions ─────────────────────────────────────────────────────────────

function OpenPositions() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const [selected, setSelected] = useState<Position | null>(null);
  const { data, isLoading, error } = useQuery({
    queryKey: ["positions-open", strategyId],
    queryFn: () =>
      fetchPositions({ strategyId: strategyId!, status: "open" }),
    enabled: !!strategyId,
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Open Positions
      </h2>
      {isLoading && <Skeleton rows={3} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && data?.length === 0 && (
        <p className="text-sm text-gray-400">No open positions</p>
      )}
      {!isLoading && !error && data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="pb-1 pr-3">Coin</th>
                <th className="pb-1 pr-3">Opened</th>
                <th className="pb-1 pr-3 text-right">Spot entry</th>
                <th className="pb-1 pr-3 text-right">Perp entry</th>
                <th className="pb-1 pr-3 text-right">Spot now</th>
                <th className="pb-1 pr-3 text-right">Perp unreal</th>
                <th className="pb-1 pr-3 text-right">Funding</th>
                <th className="pb-1 pr-3 text-right">Fees</th>
                <th className="pb-1 pr-3 text-right">Slip</th>
                <th className="pb-1 pr-3 text-right">Net P/L</th>
                <th className="pb-1 text-right">BE date</th>
              </tr>
            </thead>
            <tbody>
              {data.map((p, idx) => (
                <tr
                  key={p.id}
                  className={`cursor-pointer border-b border-gray-50 hover:bg-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                  }`}
                  onClick={() => setSelected(p)}
                  title="Click to see funding history"
                >
                  <td className="py-1 pr-3 font-medium">{p.coin}</td>
                  <td className="py-1 pr-3 text-gray-500">
                    {formatRelative(p.opened_at, now)}
                  </td>
                  <td className="py-1 pr-3 text-right">
                    {formatCurrency(p.spot_units * p.entry_spot_price)}
                  </td>
                  <td className="py-1 pr-3 text-right">
                    {formatCurrency(Math.abs(p.perp_units) * p.entry_perp_price)}
                  </td>
                  <td className="py-1 pr-3 text-right">
                    {p.spot_value_now != null
                      ? formatCurrency(p.spot_value_now)
                      : "—"}
                  </td>
                  <td
                    className={`py-1 pr-3 text-right ${
                      p.perp_unrealized == null
                        ? "text-gray-400"
                        : p.perp_unrealized > 0
                          ? "text-green-600"
                          : p.perp_unrealized < 0
                            ? "text-red-500"
                            : "text-gray-400"
                    }`}
                  >
                    {p.perp_unrealized != null
                      ? formatCurrency(p.perp_unrealized)
                      : "—"}
                  </td>
                  <td
                    className={`py-1 pr-3 text-right ${
                      p.funding_collected >= 0
                        ? "text-green-600"
                        : "text-red-500"
                    }`}
                  >
                    {formatCurrencyPrecise(p.funding_collected)}
                  </td>
                  <td className="py-1 pr-3 text-right text-red-500">
                    {formatCurrencyPrecise(p.fees_paid)}
                  </td>
                  <td className="py-1 pr-3 text-right text-red-500">
                    {p.slippage_cost !== null ? formatCurrency(p.slippage_cost) : "—"}
                  </td>
                  <td
                    className={`py-1 pr-3 text-right ${
                      p.net_mtm == null
                        ? "text-gray-400"
                        : p.net_mtm > 0
                          ? "text-green-600"
                          : p.net_mtm < 0
                            ? "text-red-500"
                            : "text-gray-400"
                    }`}
                  >
                    {p.net_mtm != null ? formatCurrency(p.net_mtm) : "—"}
                  </td>
                  <td className={`py-1 text-right ${beColor(p.breakeven_at, now)}`}>
                    {formatRelativeFuture(p.breakeven_at, now)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selected && (
        <PositionDetailsModal
          position={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

// ── Recent signals ─────────────────────────────────────────────────────────────

const ACTION_COLOR: Record<string, string> = {
  OPEN: "text-green-600",
  CLOSE: "text-red-500",
  NONE: "text-gray-400",
};

function RecentSignals() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const { data, isLoading, error } = useQuery({
    queryKey: ["signals", strategyId],
    queryFn: () => fetchSignals({ strategyId: strategyId!, limit: 20 }),
    enabled: !!strategyId,
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Recent Signals
      </h2>
      {isLoading && <Skeleton rows={3} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="pb-1 pr-3">Time</th>
                <th className="pb-1 pr-3">Coin</th>
                <th className="pb-1 pr-3">Action</th>
                <th className="pb-1 pr-3 text-right">Value</th>
                <th className="pb-1 text-right">Regime</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((s, idx) => (
                <tr
                  key={s.id}
                  className={`border-b border-gray-50 hover:bg-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                  }`}
                >
                  <td className="py-1 pr-3 text-gray-500">
                    {formatRelative(s.ts, now)}
                  </td>
                  <td className="py-1 pr-3 font-medium">{s.coin}</td>
                  <td
                    className={`py-1 pr-3 font-semibold ${ACTION_COLOR[s.action] ?? ""}`}
                  >
                    {s.action}
                  </td>
                  <td className="py-1 pr-3 text-right font-mono">
                    {formatNumber(s.signal_value, 4)}
                  </td>
                  <td className="py-1 text-right">
                    {s.regime_pass ? (
                      <span className="text-green-600">pass</span>
                    ) : (
                      <span className="text-red-400">fail</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Recent fills ───────────────────────────────────────────────────────────────

function RecentFills() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const { data, isLoading, error } = useQuery({
    queryKey: ["positions-recent", strategyId],
    queryFn: () => fetchPositions({ strategyId: strategyId!, limit: 10 }),
    enabled: !!strategyId,
  });

  const fills: (Fill & { coin: string })[] = (data ?? [])
    .flatMap((p) => p.fills.map((f) => ({ ...f, coin: p.coin })))
    .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
    .slice(0, 20);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Recent Fills
      </h2>
      {isLoading && <Skeleton rows={4} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="pb-1 pr-3">Time</th>
                <th className="pb-1 pr-3">Coin</th>
                <th className="pb-1 pr-3">Leg</th>
                <th className="pb-1 pr-3">Side</th>
                <th className="pb-1 pr-3 text-right">Qty</th>
                <th className="pb-1 pr-3 text-right">Price</th>
                <th className="pb-1 text-right">Fee</th>
              </tr>
            </thead>
            <tbody>
              {fills.map((f, idx) => (
                <tr
                  key={f.id}
                  className={`border-b border-gray-50 hover:bg-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                  }`}
                >
                  <td className="py-1 pr-3 text-gray-500">
                    {formatRelative(f.ts, now)}
                  </td>
                  <td className="py-1 pr-3 font-medium">{f.coin}</td>
                  <td className="py-1 pr-3">{f.leg}</td>
                  <td
                    className={`py-1 pr-3 font-semibold ${
                      f.side === "BUY" ? "text-green-600" : "text-red-500"
                    }`}
                  >
                    {f.side}
                  </td>
                  <td className="py-1 pr-3 text-right font-mono">
                    {formatQty(f.qty)}
                  </td>
                  <td className="py-1 pr-3 text-right">
                    {formatCurrency(f.price)}
                  </td>
                  <td className="py-1 text-right text-red-400">
                    {formatCurrencyPrecise(f.fee)}
                  </td>
                </tr>
              ))}
              {fills.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-2 text-center text-gray-400">
                    No fills yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Recent events ──────────────────────────────────────────────────────────────

const LEVEL_COLOR: Record<string, string> = {
  ERROR: "text-red-600",
  WARNING: "text-yellow-600",
  INFO: "text-blue-600",
  DEBUG: "text-gray-400",
};

function RecentEvents() {
  const now = useNow();
  const { data, isLoading, error } = useQuery({
    queryKey: ["events"],
    queryFn: () => fetchEvents({ limit: 20 }),
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Recent Events
      </h2>
      {isLoading && <Skeleton rows={4} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="pb-1 pr-3">Time</th>
                <th className="pb-1 pr-3">Level</th>
                <th className="pb-1 pr-3">Source</th>
                <th className="pb-1 pr-3">Kind</th>
                <th className="pb-1">Message</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((e, idx) => (
                <tr
                  key={e.id}
                  className={`border-b border-gray-50 hover:bg-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                  }`}
                >
                  <td className="py-1 pr-3 text-gray-500">
                    {formatRelative(e.ts, now)}
                  </td>
                  <td
                    className={`py-1 pr-3 font-semibold ${LEVEL_COLOR[e.level] ?? "text-gray-600"}`}
                  >
                    {e.level}
                  </td>
                  <td className="py-1 pr-3 text-gray-600">{e.source}</td>
                  <td className="py-1 pr-3 font-mono text-gray-500">
                    {e.kind}
                  </td>
                  <td className="py-1 text-gray-700">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── AlertBanner ───────────────────────────────────────────────────────────────

function AlertBanner() {
  const strategyId = useActiveStrategyId();
  const now = useNow();
  const [open, setOpen] = useState<boolean | null>(null); // null = use default

  const { data, isError } = useQuery({
    queryKey: ["alerts", strategyId],
    queryFn: () => fetchAlerts({ strategyId: strategyId! }),
    enabled: strategyId != null,
    refetchInterval: 30_000,
  });

  if (isError || !data || data.length === 0) return null;

  const hasError = data.some((a: Alert) => a.severity === "ERROR");
  // Default: expanded if any ERROR, collapsed if only WARNINGs
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

// ── Dashboard page ─────────────────────────────────────────────────────────────

export { Header };

export default function Dashboard() {
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="dashboard" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <AlertBanner />
        <EquityCard />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <div className="md:col-span-2">
            <OpenPositions />
          </div>
          <RecentSignals />
        </div>
        <RecentFills />
        <RecentEvents />
      </main>
    </div>
  );
}
