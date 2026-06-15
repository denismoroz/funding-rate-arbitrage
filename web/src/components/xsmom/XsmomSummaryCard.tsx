import { useXsmomSummary } from "../../lib/useXsmom";
import { formatCurrency } from "../../lib/format";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";

function StatCell({
  label,
  value,
  className = "",
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div className="flex flex-col items-start">
      <span className="text-[11px] text-gray-400 uppercase tracking-wide">{label}</span>
      <span className={`text-sm font-semibold font-mono ${className}`}>{value}</span>
    </div>
  );
}

export function XsmomSummaryCard() {
  const { data, isLoading, error } = useXsmomSummary();

  // 503 = xsmom engine not configured
  const isUnconfigured =
    error instanceof Error && error.message.startsWith("503");

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">XSMOM Summary</h2>

      {isLoading && <Skeleton rows={2} />}

      {error instanceof Error && !isUnconfigured && (
        <ErrorMsg message={error.message} />
      )}

      {isUnconfigured && (
        <p className="text-sm text-gray-400 italic">
          XSMOM engine not configured — start the strategy to see summary data.
        </p>
      )}

      {!isLoading && !error && data && (
        <div className="flex flex-wrap gap-6">
          <StatCell label="Cash" value={formatCurrency(data.cash)} className="text-sky-600" />
          <StatCell label="Long total" value={formatCurrency(data.long_total)} className="text-emerald-600" />
          <StatCell label="Short total" value={formatCurrency(data.short_total)} className="text-rose-600" />
          <StatCell
            label="PnL"
            value={data.pnl_total != null ? formatCurrency(data.pnl_total) : "—"}
            className={
              data.pnl_total == null
                ? "text-gray-400"
                : data.pnl_total >= 0
                  ? "text-green-600"
                  : "text-red-500"
            }
          />
          <StatCell label="Long positions" value={String(data.n_long)} className="text-gray-700" />
          <StatCell label="Short positions" value={String(data.n_short)} className="text-gray-700" />
        </div>
      )}
    </div>
  );
}
