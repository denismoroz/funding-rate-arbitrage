import { useState } from "react";
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
  fetchFarbPositions,
  fetchFundingHistory,
  fetchMarginState,
  closeFarbPosition,
  closeAllFarbPositions,
  tsMsToDate,
  type FarbPosition,
  type MarginFpAssessment,
  type MarginStatus,
} from "../lib/api";
import { formatCurrency, formatCurrencyPrecise, formatQty, formatRelative, formatNumber, formatHoursAsDH } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Skeleton } from "./ui/Skeleton";
import { ErrorMsg } from "./ui/ErrorMsg";

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

const MARGIN_STATUS_CLASS: Record<MarginStatus, string> = {
  healthy: "bg-gray-100 text-gray-700",
  warning: "bg-amber-100 text-amber-700",
  forced_close: "bg-rose-100 text-rose-700",
  liquidation_imminent: "bg-red-200 text-red-800",
};

function MarginRatioBadge({ fp }: { fp: MarginFpAssessment }) {
  const cls = MARGIN_STATUS_CLASS[fp.status] ?? "bg-gray-100 text-gray-700";
  const ratioStr = Number.isFinite(fp.virtual_ratio) ? `${fp.virtual_ratio.toFixed(2)}×` : "∞";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-mono font-semibold ${cls}`}
      title={
        `margin ratio (watchdog)\n` +
        `equity ${fp.virtual_equity_usdc.toFixed(2)} USDC / maint ${fp.virtual_maintenance_usdc.toFixed(2)} USDC\n` +
        `status: ${fp.status}`
      }
    >
      mr {ratioStr}
    </span>
  );
}

function FarbPositionCard({
  p,
  now,
  onSelect,
  margin,
}: {
  p: FarbPosition;
  now: number;
  onSelect: (fp: FarbPosition) => void;
  margin: MarginFpAssessment | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const queryClient = useQueryClient();
  const strategyId = useActiveStrategyId();

  const closeMutation = useMutation({
    mutationFn: () => closeFarbPosition(p.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["farb-positions-open", strategyId] });
      queryClient.invalidateQueries({ queryKey: ["farb-positions-active", strategyId] });
      queryClient.invalidateQueries({ queryKey: ["equity-summary"] });
    },
    onError: (err: Error) => {
      alert(`Ошибка закрытия ${p.coin} #${p.id}: ${err.message}`);
    },
  });

  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm(`Закрыть ${p.coin} FP#${p.id}?`)) return;
    closeMutation.mutate();
  };

  const leverage = p.leverage;

  // Actual APR from accrued funding vs capital, annualized.
  // Need ≥1h held so first funding tick has happened; otherwise factor explodes.
  const actualApr: { gross: number | null; net: number | null } =
    p.capital_usdc > 0 && p.hours_held != null && p.hours_held >= 1
      ? {
          gross: (p.funding_usdc / p.capital_usdc) * (8760 / p.hours_held),
          net: ((p.funding_usdc - p.fees_usdc) / p.capital_usdc) * (8760 / p.hours_held),
        }
      : { gross: null, net: null };

  const aprColor = (v: number | null) =>
    v == null ? "text-gray-400" : v >= 0 ? "text-green-600" : "text-rose-500";

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
        {margin && <MarginRatioBadge fp={margin} />}

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
              <span className="text-gray-700">{formatHoursAsDH(p.breakeven_hours_remaining)}</span>
            )}
          </span>
        )}
        {actualApr.gross != null && (
          <span className="text-xs">
            <span className="text-gray-400">apr </span>
            <span className={`font-mono font-semibold ${aprColor(actualApr.gross)}`}>
              {formatNumber(actualApr.gross * 100, 2)}%
            </span>
          </span>
        )}
        {p.consec_negative_hours != null && p.consec_negative_hours > 0 && (
          <span className={`text-xs ${p.consec_negative_hours > 24 ? "text-amber-600 font-semibold" : "text-gray-500"}`}>
            neg {p.consec_negative_hours}h
          </span>
        )}

        {/* close button — only for OPEN positions */}
        {p.state === "OPEN" && (
          <button
            type="button"
            className="rounded border border-rose-300 bg-rose-50 px-2 py-0.5 text-xs text-rose-700 hover:bg-rose-100 disabled:opacity-50"
            disabled={closeMutation.isPending}
            onClick={handleClose}
          >
            {closeMutation.isPending ? "…" : "Close"}
          </button>
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
                  <span className="text-gray-700">{formatHoursAsDH(p.breakeven_hours_remaining)}</span>
                )}
              </>
            )}
            {(actualApr.gross != null || actualApr.net != null) && (
              <>
                <span className="text-gray-400 mx-1">·</span>
                <span className="text-gray-400">actual gross </span>
                <span className={aprColor(actualApr.gross)}>
                  {actualApr.gross != null ? `${formatNumber(actualApr.gross * 100, 2)}%` : "—"}
                </span>
                <span className="text-gray-400 mx-1">/</span>
                <span className="text-gray-400">net </span>
                <span className={aprColor(actualApr.net)}>
                  {actualApr.net != null ? `${formatNumber(actualApr.net * 100, 2)}%` : "—"}
                </span>
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

export function OpenFarbPositions() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<FarbPosition | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["farb-positions-open", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!, "open"),
    enabled: !!strategyId,
    refetchInterval: 30_000,
  });

  const { data: marginState } = useQuery({
    queryKey: ["margin-state"],
    queryFn: fetchMarginState,
    refetchInterval: 15_000,
    retry: false,
  });

  const marginByFpId = new Map<number, MarginFpAssessment>(
    (marginState?.per_fp ?? []).map((fp) => [fp.farb_position_id, fp]),
  );

  const closeAllMutation = useMutation({
    mutationFn: () => closeAllFarbPositions(strategyId!),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["farb-positions-open", strategyId] });
      queryClient.invalidateQueries({ queryKey: ["farb-positions-active", strategyId] });
      queryClient.invalidateQueries({ queryKey: ["equity-summary"] });
      if (result.failed.length > 0) {
        const lines = result.failed.map((f) => `${f.coin} #${f.id}: ${f.reason}`).join("\n");
        alert(`Не удалось закрыть:\n${lines}`);
      }
    },
  });

  const handleCloseAll = () => {
    if (!data || data.length === 0) return;
    if (!window.confirm(`Закрыть ВСЕ ${data.length} открытые позиции?`)) return;
    closeAllMutation.mutate();
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-700">Open Positions</h2>
        {data && data.length > 0 && (
          <button
            type="button"
            className="rounded border border-rose-300 bg-rose-50 px-2 py-1 text-xs font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-50"
            disabled={closeAllMutation.isPending}
            onClick={handleCloseAll}
          >
            {closeAllMutation.isPending ? "Closing…" : "Close ALL"}
          </button>
        )}
      </div>
      {closeAllMutation.isError && (
        <p className="mb-2 text-xs text-red-600">{(closeAllMutation.error as Error).message}</p>
      )}
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
              margin={marginByFpId.get(p.id)}
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
