import { useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import {
  fetchEquity,
  fetchEquitySummary,
  fetchStrategies,
  resetEquity,
  tsMsToDate,
} from "../lib/api";
import { formatCurrency } from "../lib/format";
import { Skeleton } from "./ui/Skeleton";
import { ErrorMsg } from "./ui/ErrorMsg";

/** Default equity-chart window when no baseline has been set (35 days). */
const DEFAULT_WINDOW_MS = 35 * 24 * 60 * 60 * 1000;

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
        <p className={d.funding_cum < 0 ? "text-red-500" : "text-green-600"}>funding: ${d.funding_cum.toFixed(6)}</p>
      )}
      {d.fees_cum != null && (
        <p className="text-red-500">fees: ${d.fees_cum.toFixed(6)}</p>
      )}
      <p className="text-gray-400">{tsMsToDate(d.ts_ms).toLocaleTimeString()}</p>
    </div>
  );
}

export function EquityCard() {
  const queryClient = useQueryClient();

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const strategy = strategies?.find((s) => s.status !== "stopped");
  const strategyId = strategy?.id;

  // Chart start: explicit baseline if set, else default to 35 days ago.
  const baseline =
    typeof strategy?.params_json?.equity_baseline_ms === "number"
      ? (strategy.params_json.equity_baseline_ms as number)
      : Date.now() - DEFAULT_WINDOW_MS;

  const { data: stratData, isLoading, error } = useQuery({
    queryKey: ["equity", strategyId],
    // Hourly downsample so the chart spans the full history instead of the
    // last ~33h a raw per-minute limit=2000 would clip to.
    queryFn: () => fetchEquity(strategyId!, { limit: 2000, bucketMs: 3_600_000 }),
    enabled: !!strategyId,
  });

  const { data: summary } = useQuery({
    queryKey: ["equity-summary"],
    queryFn: fetchEquitySummary,
    refetchInterval: 30_000,
  });

  const resetMutation = useMutation({
    mutationFn: () => resetEquity(strategyId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["equity"] });
    },
    onError: (err: Error) => {
      toast.error("Reset failed", { description: err.message });
    },
  });

  function onReset() {
    if (
      window.confirm(
        "Reset equity chart start to now? Older history will be hidden.",
      )
    ) {
      resetMutation.mutate();
    }
  }

  const slice: ChartPoint[] = useMemo(() => {
    return (stratData ?? [])
      .filter((s) => s.ts_ms >= baseline)
      .map<ChartPoint>((s) => ({
        ts_ms: s.ts_ms,
        value: s.total_equity,
        funding_cum: s.funding_cum,
        fees_cum: s.fees_cum,
      }));
  }, [stratData, baseline]);

  const latestStrat = stratData && stratData.length > 0 ? stratData[stratData.length - 1] : undefined;
  const totalDisplay = latestStrat?.total_equity;

  const { yDecimals, yDomain, chartTitle, multiDay } = useMemo(() => {
    if (slice.length === 0) {
      return {
        yDecimals: 2,
        yDomain: ["auto", "auto"] as [string, string],
        chartTitle: "Equity",
        multiDay: false,
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
    const spanDays = spanHours / 24;
    const spanMinutes = spanMs / (1000 * 60);
    let title: string;
    if (spanDays >= 2) {
      title = `Equity (last ${Math.floor(spanDays)}d)`;
    } else if (spanHours >= 1) {
      title = `Equity (last ${Math.floor(spanHours)}h)`;
    } else {
      const mins = Math.floor(spanMinutes / 5) * 5 || Math.floor(spanMinutes);
      title = `Equity (last ${mins}m)`;
    }

    return { yDecimals: dec, yDomain: domain, chartTitle: title, multiDay: spanDays >= 1 };
  }, [slice]);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between gap-x-3">
        <h2 className="shrink-0 text-sm font-semibold text-gray-700">
          {chartTitle}
        </h2>
        <div className="flex items-baseline gap-x-3">
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
                <span className={`font-mono ${latestStrat.funding_cum < 0 ? "text-red-500" : "text-green-600"}`}>
                  funding ${latestStrat.funding_cum.toFixed(6)}
                </span>
                <span className="text-red-500 font-mono">
                  fees ${latestStrat.fees_cum.toFixed(6)}
                </span>
              </>
            )}
          </div>
        )}
          <button
            type="button"
            onClick={onReset}
            disabled={resetMutation.isPending || !strategyId}
            className="shrink-0 whitespace-nowrap rounded border border-gray-300 bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
          >
            {resetMutation.isPending ? "Resetting…" : "Reset start"}
          </button>
        </div>
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
                multiDay
                  ? tsMsToDate(v).toLocaleDateString([], {
                      month: "short",
                      day: "numeric",
                    })
                  : tsMsToDate(v).toLocaleTimeString([], {
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
