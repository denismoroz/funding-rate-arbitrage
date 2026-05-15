import { useEffect, useState } from "react";

/**
 * Returns a tick-updating `now` (ms since epoch). Use it as an argument to
 * `formatRelative(iso, now)` so relative timestamps re-render on the interval
 * instead of freezing at first paint.
 */
export function useNow(intervalMs: number = 30_000): number {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
