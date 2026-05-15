export type LiveEventKind =
  | "engine.started"
  | "engine.stopping"
  | "position.opened"
  | "position.closed"
  | "tick.completed";

export interface LiveEvent {
  ts: string;
  level: "INFO" | "WARNING" | "ERROR" | "DEBUG";
  source: string;
  kind: LiveEventKind | string; // open for future kinds
  message: string;
  payload_json: Record<string, unknown> | null;
}
