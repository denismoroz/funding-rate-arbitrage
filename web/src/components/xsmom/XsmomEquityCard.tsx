import { useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { tsMsToDate } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import {
  useXsmomEquity,
  useXsmomParams,
  useResetXsmomEquity,
  useXsmomSummary,
} from "../../lib/useXsmom";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";

type ChartPoint = {
  ts_ms: number;
  value: number;
};

function XsmomEquityTooltip({
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
      <p className="text-gray-400">{tsMsToDate(d.ts_ms).toLocaleTimeString()}</p>
    </div>
  );
}

export function XsmomEquityCard() {
  const { data: rows, isLoading, error } = useXsmomEquity();
  const { data: paramsData } = useXsmomParams();
  const { data: summary } = useXsmomSummary();
  const resetMutation = useResetXsmomEquity();

  const baseline =
    typeof paramsData?.params?.equity_baseline_ms === "number"
      ? paramsData.params.equity_baseline_ms
      : undefined;

  const points: ChartPoint[] = useMemo(() => {
    const all = (rows ?? []).map<ChartPoint>((s) => ({
      ts_ms: s.ts_ms,
      value: s.cash + s.perp_unrealized,
    }));
    return baseline != null ? all.filter((d) => d.ts_ms >= baseline) : all;
  }, [rows, baseline]);

  const total = points.length > 0 ? points[points.length - 1].value : undefined;

  const { yDecimals, yDomain } = useMemo(() => {
    if (points.length === 0) {
      return { yDecimals: 2, yDomain: ["auto", "auto"] as [string, string] };
    }
    const values = points.map((d) => d.value);
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
      : ["dataMin - 0.01", "dataMax + 0.01"];
    return { yDecimals: dec, yDomain: domain };
  }, [points]);

  function onReset() {
    if (
      window.confirm(
        "Reset equity chart start to now? Older history will be hidden.",
      )
    ) {
      resetMutation.mutate();
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between gap-x-3">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-gray-500">
          {total != null && (
            <span>
              Total{" "}
              <span className="text-base font-semibold text-gray-900">
                {formatCurrency(total)}
              </span>
            </span>
          )}
          {summary && (
            <>
              <span className="text-sky-600">
                free <span className="font-mono">{formatCurrency(summary.free)}</span>
              </span>
              <span className="text-amber-600">
                locked <span className="font-mono">{formatCurrency(summary.locked)}</span>
              </span>
              <span className="text-emerald-600">
                long <span className="font-mono">{formatCurrency(summary.long_total)}</span>
              </span>
              <span className="text-rose-600">
                short <span className="font-mono">{formatCurrency(summary.short_total)}</span>
              </span>
              <span
                className={`font-mono ${
                  summary.pnl_total == null
                    ? "text-gray-400"
                    : summary.pnl_total >= 0
                      ? "text-green-600"
                      : "text-red-500"
                }`}
              >
                PnL {summary.pnl_total != null ? formatCurrency(summary.pnl_total) : "—"}
              </span>
              <span className="text-gray-600">
                L <span className="font-mono">{summary.n_long}</span>
              </span>
              <span className="text-gray-600">
                S <span className="font-mono">{summary.n_short}</span>
              </span>
            </>
          )}
        </div>
        <button
          type="button"
          onClick={onReset}
          disabled={resetMutation.isPending}
          className="shrink-0 whitespace-nowrap rounded border border-gray-300 bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
        >
          {resetMutation.isPending ? "Resetting…" : "Reset start"}
        </button>
      </div>
      {isLoading && <Skeleton rows={6} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && points.length === 0 && (
        <p className="py-12 text-center text-sm text-gray-400">No equity data yet</p>
      )}
      {!isLoading && !error && points.length > 0 && (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={points}>
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
            <Tooltip content={<XsmomEquityTooltip />} />
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
