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
import { useXsmomEquity, useXsmomParams, useResetXsmomEquity } from "../../lib/useXsmom";
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
      <div className="mb-3 flex items-baseline justify-between">
        <div className="flex items-baseline gap-x-3">
          <h2 className="text-sm font-semibold text-gray-700">Equity</h2>
          {total != null && (
            <span className="text-xs text-gray-500">
              Total{" "}
              <span className="text-base font-semibold text-gray-900">
                {formatCurrency(total)}
              </span>
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onReset}
          disabled={resetMutation.isPending}
          className="rounded border border-gray-300 bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
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
              tickFormatter={(v: number) => {
                if (Math.abs(v) >= 1000) return `$${(v / 1000).toFixed(2)}k`;
                return `$${v.toFixed(2)}`;
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
