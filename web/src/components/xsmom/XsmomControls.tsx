import { useQuery } from "@tanstack/react-query";
import { fetchStrategies } from "../../lib/api";
import { useToggleXsmom, useRebalanceXsmom } from "../../lib/useXsmom";

function StrategyToggle() {
  const { mutation, strategy } = useToggleXsmom();

  if (!strategy) {
    return (
      <span className="text-xs text-gray-400 italic">
        XSMOM strategy not found
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span className="text-gray-600 font-medium">
        {strategy.name} {strategy.version}
      </span>
      <span className="text-gray-400">·</span>
      <span className="text-xs text-gray-500">
        {strategy.status === "active" ? "running" : strategy.status}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={strategy.status === "active"}
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
        title={strategy.status === "paused" ? "Resume XSMOM" : "Pause XSMOM"}
        className={[
          "relative inline-flex h-[22px] w-10 shrink-0 cursor-pointer rounded-full border-2 border-transparent",
          "transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400",
          strategy.status === "active" ? "bg-emerald-500" : "bg-slate-400",
          mutation.isPending ? "opacity-50 cursor-not-allowed" : "",
        ].join(" ")}
      >
        <span
          className={[
            "pointer-events-none inline-block h-[18px] w-[18px] rounded-full bg-white shadow-sm",
            "transform transition-transform duration-200",
            strategy.status === "active" ? "translate-x-[18px]" : "translate-x-0",
          ].join(" ")}
        />
      </button>
    </span>
  );
}

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
  // Pre-fetch strategies so StrategyToggle sees data immediately
  useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    staleTime: 10_000,
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-4">
        <h2 className="text-sm font-semibold text-gray-700">Controls</h2>
        <StrategyToggle />
        <span className="flex-1" />
        <RebalanceButton />
      </div>
    </div>
  );
}
