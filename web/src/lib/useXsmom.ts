/**
 * React-Query hooks for the XSMOM strategy.
 *
 * Query key conventions:
 *   ["xsmom-summary"]
 *   ["xsmom-positions", status?]   — status is undefined | "active" | "open" | "closed" | "failed"
 *   ["xsmom-scans", limit]
 *   ["xsmom-params"]
 *   ["strategies"]                  — shared with FRAB; used for pause/resume
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  fetchStrategies,
  fetchEquity,
  fetchXsmomSummary,
  fetchXsmomPositions,
  fetchXsmomScans,
  fetchXsmomParams,
  closeXsmomPosition,
  closeAllXsmomPositions,
  rebalanceXsmom,
  patchXsmomParams,
  resetXsmomEquity,
  pauseStrategy,
  resumeStrategy,
  previewXsmomSizing,
  type XsmomPreviewBody,
  type XsmomSizingBreakdown,
} from "./api";

// ── Strategy id ───────────────────────────────────────────────────────────────

/** Returns the Strategy row whose name === "xsmom" (for pause/resume). */
export function useXsmomStrategyId(): number | undefined {
  const q = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  return q.data?.find((s) => s.name === "xsmom")?.id;
}

// ── Queries ───────────────────────────────────────────────────────────────────

export function useXsmomSummary() {
  return useQuery({
    queryKey: ["xsmom-summary"],
    queryFn: fetchXsmomSummary,
    refetchInterval: 30_000,
    retry: false,
  });
}

export function useXsmomPositions(status?: string) {
  return useQuery({
    queryKey: ["xsmom-positions", status],
    queryFn: () => fetchXsmomPositions(status),
    refetchInterval: 30_000,
  });
}

export function useXsmomScans(limit = 10) {
  return useQuery({
    queryKey: ["xsmom-scans", limit],
    queryFn: () => fetchXsmomScans(limit),
    refetchInterval: 60_000,
  });
}

export function useXsmomParams() {
  return useQuery({
    queryKey: ["xsmom-params"],
    queryFn: fetchXsmomParams,
    staleTime: 30_000,
  });
}

export function useXsmomEquity() {
  const strategyId = useXsmomStrategyId();
  return useQuery({
    queryKey: ["xsmom-equity", strategyId],
    // Hourly downsample so the chart spans the full baseline window (~weeks)
    // instead of the last ~33h that a raw per-minute limit=2000 would clip to.
    queryFn: () => fetchEquity(strategyId!, { limit: 2000, bucketMs: 3_600_000 }),
    enabled: !!strategyId,
    refetchInterval: 60_000,
  });
}

// ── Mutations ─────────────────────────────────────────────────────────────────

/** Close a single XSMOM position; invalidates positions + summary. */
export function useCloseXsmomPosition() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => closeXsmomPosition(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["xsmom-positions"] });
      queryClient.invalidateQueries({ queryKey: ["xsmom-summary"] });
      toast.success("Position closing");
    },
    onError: (err: Error) => {
      toast.error("Close failed", { description: err.message });
    },
  });
}

/** Close all open XSMOM positions; invalidates positions + summary. */
export function useCloseAllXsmom() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => closeAllXsmomPositions(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["xsmom-positions"] });
      queryClient.invalidateQueries({ queryKey: ["xsmom-summary"] });
      toast.success("Closing all positions");
    },
    onError: (err: Error) => {
      toast.error("Close-all failed", { description: err.message });
    },
  });
}

/** Trigger XSMOM rebalance; invalidates positions + summary + scans. */
export function useRebalanceXsmom() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rebalanceXsmom(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["xsmom-positions"] });
      queryClient.invalidateQueries({ queryKey: ["xsmom-summary"] });
      queryClient.invalidateQueries({ queryKey: ["xsmom-scans"] });
    },
    onError: (err: Error) => {
      toast.error("Rebalance failed", { description: err.message });
    },
  });
}

/** Patch XSMOM params; invalidates params query. */
export function usePatchXsmomParams() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (patch: { params: Record<string, unknown> }) => patchXsmomParams(patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["xsmom-params"] });
    },
    onError: (err: Error) => {
      toast.error("Params update failed", { description: err.message });
    },
  });
}

/** Reset the XSMOM equity chart start to now; invalidates equity + params. */
export function useResetXsmomEquity() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => resetXsmomEquity(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["xsmom-equity"] });
      queryClient.invalidateQueries({ queryKey: ["xsmom-params"] });
    },
    onError: (err: Error) => {
      toast.error("Reset failed", { description: err.message });
    },
  });
}

// Re-export types needed by consumers
export type { XsmomPreviewBody, XsmomSizingBreakdown };

/**
 * Query hook for the XSMOM sizing preview endpoint.
 *
 * Enabled only when ``body`` is non-null.  The queryKey encodes all inputs so
 * React Query re-fetches automatically whenever budget_cap, n_positions, or
 * universe changes.  Callers should debounce ``body`` to avoid excessive calls.
 */
export function useXsmomSizingPreview(body: XsmomPreviewBody | null) {
  return useQuery({
    queryKey: [
      "xsmom-sizing-preview",
      body?.budget_cap,
      body?.n_positions,
      body?.universe,
    ],
    queryFn: () => previewXsmomSizing(body!),
    enabled: body !== null,
    staleTime: 10_000,
    retry: false,
  });
}

/** Pause or resume the XSMOM strategy. */
export function useToggleXsmom() {
  const queryClient = useQueryClient();
  const stratId = useXsmomStrategyId();
  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    staleTime: 10_000,
  });
  const strategy = strategies?.find((s) => s.name === "xsmom");

  const mutation = useMutation({
    mutationFn: () => {
      if (!stratId) throw new Error("XSMOM strategy not found");
      return strategy?.status === "paused"
        ? resumeStrategy(stratId)
        : pauseStrategy(stratId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (err: Error) => {
      toast.error("Toggle failed", { description: err.message });
    },
  });

  return { mutation, strategy, stratId };
}
