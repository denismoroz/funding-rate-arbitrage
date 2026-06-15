import { useXsmomPositions, useCloseAllXsmom, useRebalanceXsmom } from "../../lib/useXsmom";
import { XsmomPositionRow } from "./XsmomPositionRow";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";

function RebalanceButton() {
  const rebalanceMutation = useRebalanceXsmom();

  const handleRebalance = () => {
    if (!window.confirm("Trigger XSMOM rebalance now? This will open/close/flip positions.")) return;
    rebalanceMutation.mutate(undefined, {
      onSuccess: (result) => {
        const lines = [
          `Kept: ${result.kept.length} (${result.kept.join(", ") || "—"})`,
          `Opened: ${result.opened.length} position(s)`,
          `Dropped: ${result.dropped.length} position(s)`,
          `Flipped: ${result.flipped.length} (${result.flipped.join(", ") || "—"})`,
        ];
        alert("Rebalance complete:\n" + lines.join("\n"));
      },
    });
  };

  return (
    <button
      type="button"
      className="rounded border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
      disabled={rebalanceMutation.isPending}
      onClick={handleRebalance}
    >
      {rebalanceMutation.isPending ? "Rebalancing…" : "Rebalance"}
    </button>
  );
}

export function XsmomPositions() {
  // Default to "open" status — shows OPENED positions (the live legs)
  const status = "open";
  const { data, isLoading, error } = useXsmomPositions(status);
  const closeAllMutation = useCloseAllXsmom();

  const handleCloseAll = () => {
    if (!data || data.length === 0) return;
    if (!window.confirm(`Close ALL ${data.length} open XSMOM positions?`)) return;
    closeAllMutation.mutate();
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-700">
          Open Positions
          {data && data.length > 0 && (
            <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-normal text-gray-500">
              {data.length}
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          <RebalanceButton />
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
      </div>

      {closeAllMutation.isError && (
        <p className="mb-2 text-xs text-red-600">
          {(closeAllMutation.error as Error).message}
        </p>
      )}

      {isLoading && <Skeleton rows={3} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}

      {!isLoading && !error && data?.length === 0 && (
        <p className="text-sm text-gray-400">No open positions</p>
      )}

      {!isLoading && !error && data && data.length > 0 && (
        <div className="space-y-3">
          {data.map((p) => (
            <XsmomPositionRow key={p.id} position={p} />
          ))}
        </div>
      )}
    </div>
  );
}
