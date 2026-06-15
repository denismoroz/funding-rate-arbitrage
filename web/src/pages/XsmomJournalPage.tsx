import { Header } from "../components/Header";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useXsmomStrategyId, useXsmomPositions } from "../lib/useXsmom";
import { useNow } from "../lib/useNow";
import { Skeleton } from "../components/ui/Skeleton";
import { ErrorMsg } from "../components/ui/ErrorMsg";
import {
  formatCurrency,
  formatCurrencyPrecise,
  formatNumber,
  formatQty,
  formatRelative,
} from "../lib/format";
import { xsmomStateLabel } from "../lib/api";

const STATE_COLOR: Record<string, string> = {
  OPENED: "text-emerald-600",
  CLOSE: "text-amber-600",
  NEW: "text-sky-600",
  FAILED: "text-red-600",
  CLOSED: "text-gray-500",
};

export default function XsmomJournalPage() {
  const now = useNow();
  const strategyId = useXsmomStrategyId();
  const { status } = useLiveEvents(strategyId);

  const { data, isLoading, error } = useXsmomPositions(undefined);

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="xsmom-journal" />
      <main className="mx-auto max-w-7xl p-4 space-y-4">
        <h1 className="text-lg font-semibold text-gray-800">XSMOM Journal</h1>
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          {isLoading && <Skeleton rows={8} />}
          {error instanceof Error && <ErrorMsg message={error.message} />}
          {!isLoading && !error && data?.length === 0 && (
            <p className="text-sm text-gray-400">No XSMOM positions in DB</p>
          )}
          {!isLoading && !error && data && data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-100 text-left text-gray-500">
                    <th className="pb-1 pr-3">ID</th>
                    <th className="pb-1 pr-3">Coin</th>
                    <th className="pb-1 pr-3">Side</th>
                    <th className="pb-1 pr-3">State</th>
                    <th className="pb-1 pr-3 text-right">Score</th>
                    <th className="pb-1 pr-3">Opened</th>
                    <th className="pb-1 pr-3">Closed</th>
                    <th className="pb-1 pr-3 text-right">Held (h)</th>
                    <th className="pb-1 pr-3 text-right">Perp qty</th>
                    <th className="pb-1 pr-3 text-right">Notional</th>
                    <th className="pb-1 pr-3 text-right">Funding</th>
                    <th className="pb-1 pr-3 text-right">Fees</th>
                    <th className="pb-1 text-right">Unrealized</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((p, idx) => {
                    const stateClass = STATE_COLOR[p.state] ?? "text-gray-600";
                    const pnl = p.unrealized_pnl_usdc;
                    const pnlClass =
                      pnl == null
                        ? "text-gray-400"
                        : pnl > 0
                          ? "text-green-600"
                          : pnl < 0
                            ? "text-red-500"
                            : "text-gray-400";
                    return (
                      <tr
                        key={p.id}
                        className={`border-b border-gray-50 ${
                          idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                        }`}
                      >
                        <td className="py-1 pr-3 font-mono text-gray-400">{p.id}</td>
                        <td className="py-1 pr-3 font-medium">{p.coin}</td>
                        <td className="py-1 pr-3">{p.side}</td>
                        <td className={`py-1 pr-3 font-semibold ${stateClass}`}>
                          {xsmomStateLabel(p.state)}
                        </td>
                        <td className="py-1 pr-3 text-right">
                          {p.score != null ? formatNumber(p.score, 3) : "—"}
                        </td>
                        <td className="py-1 pr-3 text-gray-500">
                          {p.opened_at_ms != null
                            ? formatRelative(p.opened_at_ms, now)
                            : "—"}
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
                          {p.perp_leg ? formatQty(p.perp_leg.qty) : "—"}
                        </td>
                        <td className="py-1 pr-3 text-right">
                          {formatCurrency(p.notional)}
                        </td>
                        <td
                          className={`py-1 pr-3 text-right font-mono ${
                            p.funding_usdc >= 0 ? "text-green-600" : "text-red-500"
                          }`}
                        >
                          {formatCurrencyPrecise(p.funding_usdc)}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono text-gray-500">
                          {formatCurrencyPrecise(p.fees_usdc)}
                        </td>
                        <td className={`py-1 text-right ${pnlClass}`}>
                          {pnl != null ? formatCurrencyPrecise(pnl) : "—"}
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
