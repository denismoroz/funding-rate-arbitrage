import { useQuery } from "@tanstack/react-query";
import { fetchFarbPositions, type FarbPosition } from "../lib/api";
import { formatCurrency, formatNumber, formatQty, formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

const STATE_COLOR: Record<string, string> = {
  OPEN: "text-green-700",
  CLOSED: "text-gray-500",
  FAILED: "text-red-600",
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

function Skeleton({ rows = 6 }: { rows?: number }) {
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

function failureReason(fp: FarbPosition): string | null {
  const sd = fp.state_data ?? {};
  const reason = sd["failure_reason"];
  return typeof reason === "string" ? reason : null;
}

export default function Journal() {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);

  const { data, isLoading, error } = useQuery({
    queryKey: ["farb-positions-all", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!),
    enabled: !!strategyId,
    refetchInterval: 30_000,
  });

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="journal" />
      <main className="mx-auto max-w-7xl p-4 space-y-4">
        <h1 className="text-lg font-semibold text-gray-800">Journal</h1>
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          {isLoading && <Skeleton rows={8} />}
          {error instanceof Error && <ErrorMsg message={error.message} />}
          {!isLoading && !error && data?.length === 0 && (
            <p className="text-sm text-gray-400">No farb positions in DB</p>
          )}
          {!isLoading && !error && data && data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-100 text-left text-gray-500">
                    <th className="pb-1 pr-3">ID</th>
                    <th className="pb-1 pr-3">Coin</th>
                    <th className="pb-1 pr-3">State</th>
                    <th className="pb-1 pr-3">Opened</th>
                    <th className="pb-1 pr-3">Closed</th>
                    <th className="pb-1 pr-3 text-right">Held (h)</th>
                    <th className="pb-1 pr-3 text-right">Spot qty</th>
                    <th className="pb-1 pr-3 text-right">Perp qty</th>
                    <th className="pb-1 pr-3 text-right">Target APR</th>
                    <th className="pb-1 pr-3 text-right">Exit APR</th>
                    <th className="pb-1 pr-3 text-right">Unrealized</th>
                    <th className="pb-1">Failure / note</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((p, idx) => {
                    const reason = failureReason(p);
                    const stateClass = STATE_COLOR[p.state] ?? "text-gray-600";
                    return (
                      <tr
                        key={p.id}
                        className={`border-b border-gray-50 ${
                          idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                        }`}
                      >
                        <td className="py-1 pr-3 font-mono text-gray-400">{p.id}</td>
                        <td className="py-1 pr-3 font-medium">{p.coin}</td>
                        <td className={`py-1 pr-3 font-semibold ${stateClass}`}>
                          {p.state}
                        </td>
                        <td className="py-1 pr-3 text-gray-500">
                          {formatRelative(p.opened_at_ms, now)}
                        </td>
                        <td className="py-1 pr-3 text-gray-500">
                          {p.closed_at_ms != null
                            ? formatRelative(p.closed_at_ms, now)
                            : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right">
                          {p.hours_held != null ? formatNumber(p.hours_held, 1) : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono">
                          {p.legs.spot ? formatQty(p.legs.spot.qty) : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono">
                          {p.legs.perp ? formatQty(p.legs.perp.qty) : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right text-indigo-600">
                          {p.target_signal_apr != null
                            ? `${formatNumber(p.target_signal_apr * 100, 2)}%`
                            : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right text-gray-600">
                          {p.exit_signal_apr != null
                            ? `${formatNumber(p.exit_signal_apr * 100, 2)}%`
                            : "—"}
                        </td>
                        <td
                          className={`py-1 pr-3 text-right ${
                            p.unrealized_pnl_usdc == null
                              ? "text-gray-400"
                              : p.unrealized_pnl_usdc > 0
                                ? "text-green-600"
                                : p.unrealized_pnl_usdc < 0
                                  ? "text-red-500"
                                  : "text-gray-400"
                          }`}
                        >
                          {p.unrealized_pnl_usdc != null
                            ? formatCurrency(p.unrealized_pnl_usdc)
                            : "—"}
                        </td>
                        <td className="py-1 text-gray-500" title={reason ?? undefined}>
                          {reason ?? ""}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
