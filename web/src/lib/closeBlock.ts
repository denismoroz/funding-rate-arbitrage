import type { FarbPosition } from "./api";

/**
 * The red line on a position that needs to exit but cannot be closed within HL's limits:
 * "can't close — needs to: <wanted>; <reason>" (numbers come from the backend's check).
 * Null when the position is not blocked.
 */
export function closeBlockedLine(
  p: Pick<FarbPosition, "close_blocked" | "close_blocked_wanted" | "close_blocked_reason">,
): string | null {
  if (!p.close_blocked) return null;
  const wanted = p.close_blocked_wanted ?? "exit";
  const reason = p.close_blocked_reason ?? "cannot be closed within the venue's limits";
  return `can't close — needs to: ${wanted}; ${reason}`;
}
