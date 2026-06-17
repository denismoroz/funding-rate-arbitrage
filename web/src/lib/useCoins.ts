/**
 * React-Query hooks for the Coin Registry.
 *
 * Query key convention:
 *   ["coins"]  — the full registry list
 *
 * Each mutation invalidates ["coins"] on success so the table auto-refreshes.
 * Server {detail} errors are surfaced as Error.message to the caller.
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchCoins,
  addCoin,
  patchCoin,
  setCoinActive,
  deleteCoin,
  type AddCoinBody,
  type PatchCoinBody,
} from "./api";

const COINS_KEY = ["coins"] as const;

// ── Queries ───────────────────────────────────────────────────────────────────

export function useCoins() {
  return useQuery({
    queryKey: COINS_KEY,
    queryFn: fetchCoins,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

// ── Mutations ─────────────────────────────────────────────────────────────────

/** POST /api/coins — runs HL discovery server-side; returned row has active=false. */
export function useAddCoin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AddCoinBody) => addCoin(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: COINS_KEY });
    },
  });
}

/** PATCH /api/coins/{coin} — risk fields only. 409 = open position. */
export function usePatchCoin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ coin, body }: { coin: string; body: PatchCoinBody }) =>
      patchCoin(coin, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: COINS_KEY });
    },
  });
}

/** POST /api/coins/{coin}/active — toggle. 409 = not validated. */
export function useSetCoinActive() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ coin, active }: { coin: string; active: boolean }) =>
      setCoinActive(coin, active),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: COINS_KEY });
    },
  });
}

/** DELETE /api/coins/{coin} — 409 = open position. */
export function useDeleteCoin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (coin: string) => deleteCoin(coin),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: COINS_KEY });
    },
  });
}
