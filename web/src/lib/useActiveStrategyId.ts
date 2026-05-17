import { useQuery } from "@tanstack/react-query";
import { fetchStrategies } from "./api";

/**
 * Returns the id of the strategy currently bound to the engine (status === "running"),
 * or undefined while loading / if no strategy is running.
 *
 * Pollers/UI components keyed on this id should guard with `enabled: !!id`
 * so they don't fire with a stale or default value.
 */
export function useActiveStrategyId(): number | undefined {
  const q = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const running = q.data?.find((s) => s.status === "running");
  return running?.id;
}
