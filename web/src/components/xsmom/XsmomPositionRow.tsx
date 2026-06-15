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
    <tr className="border-b border-gray-50 hover:bg-gray-50">
      {/* Coin */}
      <td className="py-1 pr-3 font-medium text-gray-900">{position.coin}</td>

      {/* Side */}
      <td className="py-1 pr-3">
        <SideBadge side={position.side} />
      </td>

      {/* State */}
      <td className="py-1 pr-3">
        <StateBadge state={position.state} />
      </td>

      {/* Score */}
      <td className="py-1 pr-3 text-right font-mono text-slate-600">
        {position.score != null ? formatNumber(position.score, 3) : "—"}
      </td>

      {/* Leverage */}
      <td className="py-1 pr-3 text-right">
        {position.leverage != null ? `${position.leverage}×` : "—"}
      </td>

      {/* Held */}
      <td
        className="py-1 pr-3 text-right text-gray-600"
        title={formatRelative(position.opened_at_ms, now)}
      >
        {position.hours_held != null ? formatHoursAsDH(position.hours_held) : "—"}
      </td>

      {/* Perp qty */}
      <td className="py-1 pr-3 text-right font-mono">
        {position.perp_leg ? formatQty(position.perp_leg.qty) : "—"}
      </td>

      {/* Entry */}
      <td className="py-1 pr-3 text-right font-mono">
        {position.perp_leg ? formatCurrency(position.perp_leg.entry_price) : "—"}
      </td>

      {/* Notional */}
      <td className="py-1 pr-3 text-right">{formatCurrency(position.notional)}</td>

      {/* PnL */}
      <td className={`py-1 pr-3 text-right font-mono ${pnlClass}`}>
        {pnl != null ? formatCurrencyPrecise(pnl) : "—"}
      </td>

      {/* Funding */}
      <td className={`py-1 pr-3 text-right font-mono ${fundingClass}`}>
        {formatCurrencyPrecise(position.funding_usdc)}
      </td>

      {/* Fees */}
      <td className="py-1 pr-3 text-right font-mono text-gray-500">
        {formatCurrencyPrecise(position.fees_usdc)}
      </td>

      {/* Locked margin */}
      <td className="py-1 pr-3 text-right text-gray-500">
        {position.locked_margin_usdc > 0 ? formatCurrency(position.locked_margin_usdc) : "—"}
      </td>

      {/* Close button — only when OPENED */}
      <td className="py-1">
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
      </td>
    </tr>
  );
}
