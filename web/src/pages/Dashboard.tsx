import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
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
  fetchStrategy,
  fetchEquity,
  fetchEquitySummary,
  fetchFarbPositions,
  fetchFundingHistory,
  fetchEvents,
  fetchAlerts,
  forceHourTick,
  tsMsToDate,
  type Alert,
  type FarbPosition,
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
  ts_ms: number;
  value: number;
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
        <p className="text-green-600">funding: ${d.funding_cum.toFixed(6)}</p>
      )}
      {d.fees_cum != null && (
        <p className="text-red-500">fees: ${d.fees_cum.toFixed(6)}</p>
      )}
      <p className="text-gray-400">{tsMsToDate(d.ts_ms).toLocaleTimeString()}</p>
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

function Header({ wsStatus, route }: { wsStatus: WsStatus; route: "dashboard" | "settings" | "funding" | "journal" }) {
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

  const lastTick = eventsQ.data?.find((e) => e.kind === "tick.completed");
  // WS events still carry `ts` as ISO string; DB events have ts_ms
  const lastTickAgeMs = lastTick
    ? now - tsMsToDate(lastTick.ts_ms).getTime()
    : Infinity;
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
        <a
          href="#/funding"
          className={route === "funding" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Funding
        </a>
        <a
          href="#/journal"
          className={route === "journal" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Journal
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
            ({formatRelative(engineEvent.ts_ms, now)})
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

  const { data: stratData, isLoading, error } = useQuery({
    queryKey: ["equity", strategyId],
    queryFn: () => fetchEquity(strategyId!, { limit: 2000 }),
    enabled: !!strategyId,
  });

  const { data: summary } = useQuery({
    queryKey: ["equity-summary"],
    queryFn: fetchEquitySummary,
    refetchInterval: 30_000,
  });

  const slice: ChartPoint[] = useMemo(() => {
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const all = (stratData ?? []).map<ChartPoint>((s) => ({
      ts_ms: s.ts_ms,
      value: s.total_equity,
      funding_cum: s.funding_cum,
      fees_cum: s.fees_cum,
    }));
    const recent = all.filter((d) => d.ts_ms >= cutoff);
    return recent.length > 0 ? recent : all;
  }, [stratData]);

  const latestStrat = stratData && stratData.length > 0 ? stratData[stratData.length - 1] : undefined;
  const totalDisplay = latestStrat?.total_equity;

  const { yDecimals, yDomain, chartTitle } = useMemo(() => {
    if (slice.length === 0) {
      return {
        yDecimals: 2,
        yDomain: ["auto", "auto"] as [string, string],
        chartTitle: "Equity (last 24h)",
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

    const firstTs = slice[0].ts_ms;
    const lastTs = slice[slice.length - 1].ts_ms;
    const spanMs = lastTs - firstTs;
    const spanHours = spanMs / (1000 * 60 * 60);
    const spanMinutes = spanMs / (1000 * 60);
    let title: string;
    if (spanHours >= 23) {
      title = "Equity (last 24h)";
    } else if (spanHours >= 1) {
      title = `Equity (last ${Math.floor(spanHours)}h)`;
    } else {
      const mins = Math.floor(spanMinutes / 5) * 5 || Math.floor(spanMinutes);
      title = `Equity (last ${mins}m)`;
    }

    return { yDecimals: dec, yDomain: domain, chartTitle: title };
  }, [slice]);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-gray-700">
          {chartTitle}
        </h2>
        {(totalDisplay != null || latestStrat || summary) && (
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-gray-500">
            <span>
              Total{" "}
              <span className="text-base font-semibold text-gray-900">
                {totalDisplay != null ? formatCurrency(totalDisplay) : "—"}
              </span>
            </span>
            {summary && (
              <>
                <span className="text-emerald-600">
                  long <span className="font-mono">{formatCurrency(summary.long)}</span>
                </span>
                <span className="text-rose-600">
                  short <span className="font-mono">{formatCurrency(summary.short)}</span>
                </span>
                <span className="text-sky-600">
                  free <span className="font-mono">{formatCurrency(summary.free)}</span>
                </span>
                <span className="text-amber-600">
                  locked <span className="font-mono">{formatCurrency(summary.locked)}</span>
                </span>
                <span className="text-violet-600">
                  reserved <span className="font-mono">{formatCurrency(summary.reserved)}</span>
                </span>
              </>
            )}
            {latestStrat && (
              <>
                <span className="text-green-600 font-mono">
                  funding ${latestStrat.funding_cum.toFixed(6)}
                </span>
                <span className="text-red-500 font-mono">
                  fees ${latestStrat.fees_cum.toFixed(6)}
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
              dataKey="ts_ms"
              tickFormatter={(v: number) =>
                tsMsToDate(v).toLocaleTimeString([], {
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

// ── FarbPosition details modal ─────────────────────────────────────────────────

function FarbPositionModal({
  position,
  onClose,
}: {
  position: FarbPosition;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["funding", position.coin],
    queryFn: () => fetchFundingHistory(position.coin, { limit: 200 }),
  });

  // API returns newest-first; reverse for chronological chart
  const chronological = (data ?? []).slice().reverse();

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
              {position.coin} · recent funding rates
            </h3>
            <p className="text-xs text-gray-500">
              Opened {formatRelative(position.opened_at_ms)} · State{" "}
              <span className="font-mono">{position.state}</span>
              {position.unrealized_pnl_usdc != null && (
                <>
                  {" "}· Unrealized{" "}
                  <span className={position.unrealized_pnl_usdc >= 0 ? "text-green-600" : "text-red-500"}>
                    {formatCurrency(position.unrealized_pnl_usdc)}
                  </span>
                </>
              )}
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
              Funding rate (% APR) — last {chronological.length} ticks
            </h4>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={chronological.map((r) => ({ ts_ms: r.ts_ms, rate_apr: r.annualized_pct }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="ts_ms"
                  tickFormatter={(v: number) =>
                    tsMsToDate(v).toLocaleTimeString([], {
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
                  labelFormatter={(v: number) => tsMsToDate(v).toLocaleString()}
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

            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs md:grid-cols-4">
              {position.legs.spot && (
                <>
                  <div><span className="text-gray-500">spot qty:</span> {formatQty(position.legs.spot.qty)}</div>
                  <div><span className="text-gray-500">spot entry:</span> {formatCurrency(position.legs.spot.entry_price)}</div>
                </>
              )}
              {position.legs.perp && (
                <>
                  <div><span className="text-gray-500">perp qty:</span> {formatQty(position.legs.perp.qty)}</div>
                  <div><span className="text-gray-500">perp entry:</span> {formatCurrency(position.legs.perp.entry_price)}</div>
                </>
              )}
              {position.target_signal_apr != null && (
                <div><span className="text-gray-500">target APR:</span> {formatNumber(position.target_signal_apr * 100, 2)}%</div>
              )}
              {position.hours_held != null && (
                <div><span className="text-gray-500">held:</span> {formatNumber(position.hours_held, 1)}h</div>
              )}
              {position.consec_negative_hours != null && (
                <div><span className="text-gray-500">consec neg hrs:</span> {position.consec_negative_hours}</div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Open FarbPositions ─────────────────────────────────────────────────────────

function FarbPositionCard({
  p,
  now,
  onSelect,
}: {
  p: FarbPosition;
  now: number;
  onSelect: (fp: FarbPosition) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const leverage = p.leverage;

  const pnlColor =
    p.unrealized_pnl_usdc == null
      ? "text-gray-400"
      : p.unrealized_pnl_usdc > 0
        ? "text-green-600"
        : p.unrealized_pnl_usdc < 0
          ? "text-red-500"
          : "text-gray-400";

  const spotValue =
    p.legs.spot ? p.legs.spot.qty * p.legs.spot.entry_price : null;
  const perpValue =
    p.legs.perp ? p.legs.perp.qty * p.legs.perp.entry_price : null;
  const collateralValue =
    p.legs.collateral ? p.legs.collateral.qty : null;

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      {/* ── Header strip — always visible, click to expand ─── */}
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 rounded-lg cursor-pointer hover:bg-gray-50"
        onClick={() => setExpanded((v) => !v)}
      >
        {/* chevron */}
        <span className="text-gray-400 text-[10px] select-none w-3 shrink-0">
          {expanded ? "▼" : "▶"}
        </span>

        {/* coin + state + held + leverage */}
        <span className="font-semibold text-gray-900 text-sm">{p.coin}</span>
        <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-mono font-semibold text-indigo-700">
          {p.state}
        </span>
        <span className="text-xs text-gray-500">
          {p.hours_held != null ? `${formatNumber(p.hours_held, 1)}h` : "—"}
          <span className="ml-1 text-gray-400 text-[10px]">
            ({formatRelative(p.opened_at_ms, now)})
          </span>
        </span>
        {leverage != null && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
            {leverage}×
          </span>
        )}

        {/* capital */}
        {p.capital_usdc > 0 && (
          <span className="font-semibold text-gray-800 text-xs">
            cap {formatCurrency(p.capital_usdc)}
          </span>
        )}

        {/* spacer */}
        <span className="flex-1" />

        {/* pnl + funding + fees + break-even */}
        <span className={`font-mono text-xs ${pnlColor}`}>
          pnl {p.unrealized_pnl_usdc != null ? formatCurrencyPrecise(p.unrealized_pnl_usdc) : "—"}
        </span>
        <span className="font-mono text-xs text-green-600">
          funding ${p.funding_usdc.toFixed(6)}
        </span>
        <span className="font-mono text-xs text-gray-500">
          fees ${p.fees_usdc.toFixed(6)}
        </span>
        {p.breakeven_hours_remaining != null && (
          <span className="text-xs">
            <span className="text-gray-400">BE </span>
            {p.breakeven_hours_remaining <= 0 ? (
              <span className="text-green-600 font-semibold">done</span>
            ) : (
              <span className="text-gray-700">{formatNumber(p.breakeven_hours_remaining, 1)}h</span>
            )}
          </span>
        )}
        {p.consec_negative_hours != null && p.consec_negative_hours > 0 && (
          <span className={`text-xs ${p.consec_negative_hours > 24 ? "text-amber-600 font-semibold" : "text-gray-500"}`}>
            neg {p.consec_negative_hours}h
          </span>
        )}
      </div>

      {/* ── Expanded: APR sub-line + leg table ───────────────── */}
      {expanded && (
        <div className="border-t border-gray-100 px-3 py-2">
          {/* APR triplet sub-line */}
          <p className="mb-2 text-xs text-gray-500">
            <span className="text-gray-400">target </span>
            <span className="text-indigo-600">
              {p.target_signal_apr != null ? `${formatNumber(p.target_signal_apr * 100, 2)}%` : "—"}
            </span>
            <span className="text-gray-400 mx-1">/</span>
            <span className="text-gray-400">exit </span>
            <span className="text-rose-500">
              {p.exit_signal_apr != null ? `${formatNumber(p.exit_signal_apr * 100, 2)}%` : "—"}
            </span>
            <span className="text-gray-400 mx-1">/</span>
            <span className="text-gray-400">now </span>
            <span className="text-emerald-600">
              {p.current_signal_apr != null ? `${formatNumber(p.current_signal_apr * 100, 2)}%` : "—"}
            </span>
            {p.breakeven_hours_remaining != null && (
              <>
                <span className="text-gray-400 mx-1">·</span>
                <span className="text-gray-400">BE </span>
                {p.breakeven_hours_remaining <= 0 ? (
                  <span className="text-green-600 font-semibold">done</span>
                ) : (
                  <span className="text-gray-700">{formatNumber(p.breakeven_hours_remaining, 1)}h</span>
                )}
              </>
            )}
            <span className="text-gray-400 mx-1">·</span>
            <span className="text-gray-400">consec_neg </span>
            <span className="text-gray-700">{p.consec_negative_hours ?? 0}</span>
          </p>

          {/* Leg table */}
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-[11px] text-gray-400">
                <th className="pb-1 pr-3 font-medium">Leg</th>
                <th className="pb-1 pr-3 text-right font-medium">Qty</th>
                <th className="pb-1 pr-3 text-right font-medium">Entry</th>
                <th className="pb-1 pr-3 text-right font-medium">Value</th>
                <th className="pb-1 pr-3 text-right font-medium">PnL</th>
                <th className="pb-1 font-medium">Notes</th>
              </tr>
            </thead>
            <tbody>
              {/* spot long */}
              <tr className="border-t border-gray-50">
                <td className="py-0.5 pr-3">
                  <span className="text-indigo-500 font-medium">long</span>
                  <span className="text-gray-400 ml-1">spot</span>
                </td>
                <td className="py-0.5 pr-3 text-right font-mono text-gray-700">
                  {p.legs.spot ? formatQty(p.legs.spot.qty) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-500">
                  {p.legs.spot ? formatCurrency(p.legs.spot.entry_price) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-700">
                  {spotValue != null ? formatCurrency(spotValue) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right font-mono">
                  {p.spot_unrealized_pnl_usdc != null ? (
                    <span className={p.spot_unrealized_pnl_usdc >= 0 ? "text-green-600" : "text-rose-500"}>
                      {formatCurrencyPrecise(p.spot_unrealized_pnl_usdc)}
                    </span>
                  ) : (
                    <span className="text-gray-400">—</span>
                  )}
                </td>
                <td className="py-0.5 text-gray-400" />
              </tr>

              {/* perp short */}
              <tr className="border-t border-gray-50">
                <td className="py-0.5 pr-3">
                  <span className="text-rose-500 font-medium">short</span>
                  <span className="text-gray-400 ml-1">perp</span>
                </td>
                <td className="py-0.5 pr-3 text-right font-mono text-gray-700">
                  {p.legs.perp ? formatQty(p.legs.perp.qty) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-500">
                  {p.legs.perp ? formatCurrency(p.legs.perp.entry_price) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-700">
                  {perpValue != null ? formatCurrency(perpValue) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right font-mono">
                  {p.perp_unrealized_pnl_usdc != null ? (
                    <span className={p.perp_unrealized_pnl_usdc >= 0 ? "text-green-600" : "text-rose-500"}>
                      {formatCurrencyPrecise(p.perp_unrealized_pnl_usdc)}
                    </span>
                  ) : (
                    <span className="text-gray-400">—</span>
                  )}
                </td>
                <td className="py-0.5 text-gray-500 text-[11px]">
                  {leverage != null && (
                    <span className="mr-2">leverage {leverage}×</span>
                  )}
                  {p.locked_margin_usdc > 0 && (
                    <span>locked {formatCurrency(p.locked_margin_usdc)}</span>
                  )}
                </td>
              </tr>

              {/* collateral / margin */}
              <tr className="border-t border-gray-50">
                <td className="py-0.5 pr-3">
                  <span className="text-sky-500 font-medium">margin</span>
                  <span className="text-gray-400 ml-1">usdc</span>
                </td>
                <td className="py-0.5 pr-3 text-right font-mono text-gray-700">
                  {p.legs.collateral ? formatCurrency(p.legs.collateral.qty) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-400">$1.00</td>
                <td className="py-0.5 pr-3 text-right text-gray-700">
                  {collateralValue != null ? formatCurrency(collateralValue) : "—"}
                </td>
                <td className="py-0.5 pr-3 text-right text-gray-400">—</td>
                <td className="py-0.5 text-gray-400 text-[11px]">reserved buffer</td>
              </tr>
            </tbody>
          </table>

          {/* Funding history trigger */}
          <button
            type="button"
            className="mt-2 text-[11px] text-indigo-500 hover:text-indigo-700 underline"
            onClick={(e) => { e.stopPropagation(); onSelect(p); }}
          >
            funding history ↗
          </button>
        </div>
      )}
    </div>
  );
}

function OpenFarbPositions() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const [selected, setSelected] = useState<FarbPosition | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["farb-positions-open", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!, "open"),
    enabled: !!strategyId,
    refetchInterval: 30_000,
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
        <div className="space-y-3">
          {data.map((p) => (
            <FarbPositionCard
              key={p.id}
              p={p}
              now={now}
              onSelect={setSelected}
            />
          ))}
        </div>
      )}
      {selected && (
        <FarbPositionModal
          position={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

// ── Active (in-flight) FarbPositions ─────────────────────────────────────────

const STATE_COLOR: Record<string, string> = {
  CHECK_MARGIN: "text-yellow-600",
  OPENING_MARGIN: "text-blue-600",
  MARGIN_RESERVED: "text-blue-500",
  OPENING_LONG: "text-blue-600",
  LONG_OPENED: "text-blue-500",
  OPENING_SHORT: "text-blue-600",
  CLOSING_SHORT: "text-orange-600",
  SHORT_CLOSED: "text-orange-500",
  CLOSING_LONG: "text-orange-600",
  LONG_CLOSED: "text-orange-500",
  RELEASING_MARGIN: "text-orange-500",
};

function ActiveFarbPositions() {
  const now = useNow();
  const strategyId = useActiveStrategyId();

  const { data, isLoading, error } = useQuery({
    queryKey: ["farb-positions-active", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!, "active"),
    enabled: !!strategyId,
    refetchInterval: 15_000,
  });

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <Skeleton rows={2} />
      </div>
    );
  }
  if (error instanceof Error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <ErrorMsg message={error.message} />
      </div>
    );
  }
  if (!data || data.length === 0) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 shadow-sm">
      <h2 className="mb-2 text-sm font-semibold text-amber-800">
        In-Flight ({data.length})
      </h2>
      <div className="space-y-1">
        {data.map((p) => (
          <div key={p.id} className="flex items-center gap-3 text-xs">
            <span className="font-medium text-gray-800 w-12">{p.coin}</span>
            <span className={`font-mono font-semibold ${STATE_COLOR[p.state] ?? "text-gray-600"}`}>
              {p.state}
            </span>
            <span className="text-gray-500">
              {formatRelative(p.opened_at_ms, now)}
            </span>
          </div>
        ))}
      </div>
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
                    {formatRelative(e.ts_ms, now)}
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

// ── Signals strip ─────────────────────────────────────────────────────────────

const HOURS_PER_YEAR = 8760;

function SignalCard({ coin, entryThreshold, exitThreshold, window }: {
  coin: string;
  entryThreshold: number;
  exitThreshold: number;
  window: number;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["funding-recent", coin, window],
    queryFn: () => fetchFundingHistory(coin, { limit: window }),
    refetchInterval: 60_000,
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
    </div>
  );
}

function SignalsStrip() {
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
  const window = (params?.signal_window_hours as number | undefined) ?? 12;

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
            entry {(entryThreshold * 100).toFixed(0)}% · exit {(exitThreshold * 100).toFixed(0)}% · window {window}h
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
            window={window}
          />
        ))}
      </div>
    </div>
  );
}

function AlertBanner() {
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
        <ActiveFarbPositions />
        <EquityCard />
        <SignalsStrip />
        <OpenFarbPositions />
        <RecentEvents />
      </main>
    </div>
  );
}
