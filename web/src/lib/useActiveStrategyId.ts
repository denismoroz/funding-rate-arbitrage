import { useQuery } from "@tanstack/react-query";
import { fetchStrategies } from "./api";

/**
 * Returns the id of the strategy currently shown on the dashboard.
 *
 * "Selected" = any non-terminal status (active / running / paused / idle).
 * Pause must NOT hide the strategy — dashboard still needs to render its
 * equity, positions, and the toggle that resumes it.
 */
export function useActiveStrategyId(): number | undefined {
  const q = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const selected = q.data?.find((s) => s.status !== "stopped");
  return selected?.id;
}
