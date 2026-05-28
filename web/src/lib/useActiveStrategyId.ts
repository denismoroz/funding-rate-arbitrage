import { useQuery } from "@tanstack/react-query";
import { fetchStrategies } from "./api";

/**
 * Returns the id of the active strategy (status in {"active", "running"}),
 * or undefined while loading / if none exists.
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
  const active = q.data?.find((s) => s.status === "active" || s.status === "running");
  return active?.id;
}
