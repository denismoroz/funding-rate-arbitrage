import { useQuery } from "@tanstack/react-query";
import { fetchFarbPositions } from "../lib/api";
import { formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Skeleton } from "./ui/Skeleton";
import { ErrorMsg } from "./ui/ErrorMsg";

const STATE_COLOR: Record<string, string> = {
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

export function ActiveFarbPositions() {
  const now = useNow();
  const strategyId = useActiveStrategyId();

  const { data, isLoading, error } = useQuery({
    queryKey: ["farb-positions-active", strategyId],
    queryFn: () => fetchFarbPositions(strategyId!, "active"),
    enabled: !!strategyId,
    refetchInterval: 15_000,
  });

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <Skeleton rows={2} />
      </div>
    );
  }
  if (error instanceof Error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <ErrorMsg message={error.message} />
      </div>
    );
  }
  if (!data || data.length === 0) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 shadow-sm">
      <h2 className="mb-2 text-sm font-semibold text-amber-800">
        In-Flight ({data.length})
      </h2>
      <div className="space-y-1">
        {data.map((p) => (
          <div key={p.id} className="flex items-center gap-3 text-xs">
            <span className="font-medium text-gray-800 w-12">{p.coin}</span>
            <span className={`font-mono font-semibold ${STATE_COLOR[p.state] ?? "text-gray-600"}`}>
              {p.state}
            </span>
            <span className="text-gray-500">
              {formatRelative(p.opened_at_ms, now)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
