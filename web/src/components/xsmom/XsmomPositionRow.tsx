import { type XsmomPosition, isXsmomOpen, xsmomStateLabel } from "../../lib/api";
import { useCloseXsmomPosition } from "../../lib/useXsmom";
import { formatCurrency, formatCurrencyPrecise, formatQty, formatRelative, formatNumber, formatHoursAsDH } from "../../lib/format";
import { useNow } from "../../lib/useNow";

function SideBadge({ side }: { side: "long" | "short" }) {
  const cls =
    side === "long"
      ? "bg-indigo-100 text-indigo-700"
      : "bg-rose-100 text-rose-700";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold font-mono ${cls}`}>
      {side}
    </span>
  );
}

function StateBadge({ state }: { state: string }) {
  let cls = "bg-gray-100 text-gray-600";
  if (state === "OPENED") cls = "bg-emerald-100 text-emerald-700";
  else if (state === "CLOSE") cls = "bg-amber-100 text-amber-700";
  else if (state === "FAILED") cls = "bg-red-100 text-red-700";
  else if (state === "NEW") cls = "bg-sky-100 text-sky-700";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold font-mono ${cls}`}>
      {xsmomStateLabel(state)}
    </span>
  );
}

export function XsmomPositionRow({ position }: { position: XsmomPosition }) {
  const now = useNow();
  const closeMutation = useCloseXsmomPosition();

  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!window.confirm(`Close ${position.coin} XSMOM #${position.id}?`)) return;
    closeMutation.mutate(position.id);
  };

  const pnl = position.unrealized_pnl_usdc;
  const pnlClass =
    pnl == null
      ? "text-gray-400"
      : pnl > 0
        ? "text-green-600"
        : pnl < 0
          ? "text-red-500"
          : "text-gray-400";

  const fundingClass = position.funding_usdc >= 0 ? "text-green-600" : "text-red-500";

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
        {/* Coin + badges */}
        <span className="font-semibold text-gray-900 text-sm w-16 shrink-0">
          {position.coin}
        </span>
        <SideBadge side={position.side} />
        <StateBadge state={position.state} />

        {/* Score */}
        {position.score != null && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-mono text-slate-600">
            score {formatNumber(position.score, 3)}
          </span>
        )}

        {/* Leverage */}
        {position.leverage != null && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
            {position.leverage}×
          </span>
        )}

        {/* Time held */}
        {position.hours_held != null && (
          <span className="text-xs text-gray-500">
            {formatHoursAsDH(position.hours_held)}
            <span className="ml-1 text-gray-400 text-[10px]">
              ({formatRelative(position.opened_at_ms, now)})
            </span>
          </span>
        )}

        {/* Notional */}
        <span className="text-xs text-gray-600">
          <span className="text-gray-400">notional </span>
          {formatCurrency(position.notional)}
        </span>

        {/* Perp leg: qty + entry */}
        {position.perp_leg && (
          <span className="text-xs text-gray-600 font-mono">
            {formatQty(position.perp_leg.qty)} @ {formatCurrency(position.perp_leg.entry_price)}
          </span>
        )}

        <span className="flex-1" />

        {/* PnL */}
        <span className={`font-mono text-xs ${pnlClass}`}>
          pnl {pnl != null ? formatCurrencyPrecise(pnl) : "—"}
        </span>

        {/* Funding */}
        <span className={`font-mono text-xs ${fundingClass}`}>
          funding ${position.funding_usdc.toFixed(6)}
        </span>

        {/* Fees */}
        <span className="font-mono text-xs text-gray-500">
          fees ${position.fees_usdc.toFixed(6)}
        </span>

        {/* Locked margin */}
        {position.locked_margin_usdc > 0 && (
          <span className="text-xs text-gray-500">
            <span className="text-gray-400">locked </span>
            {formatCurrency(position.locked_margin_usdc)}
          </span>
        )}

        {/* Close button — only when OPENED */}
        {isXsmomOpen(position.state) && (
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
    </div>
  );
}
