import { useRebalanceXsmom } from "../../lib/useXsmom";

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

export function XsmomControls() {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-4">
        <h2 className="text-sm font-semibold text-gray-700">Controls</h2>
        <span className="flex-1" />
        <RebalanceButton />
      </div>
    </div>
  );
}
