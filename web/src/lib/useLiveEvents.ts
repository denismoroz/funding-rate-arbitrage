import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { LiveEvent } from "./ws";

export type WsStatus = "connecting" | "open" | "closed";

const MAX_BACKOFF_MS = 30_000;

export function useLiveEvents(strategyId?: number): { status: WsStatus; lastEvent: LiveEvent | null } {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);

  // Use refs so the effect closure always sees the current values without
  // needing to be recreated on every state change.
  const strategyIdRef = useRef<number | undefined>(strategyId);
  strategyIdRef.current = strategyId;
  const backoffRef = useRef<number>(1_000);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const unmountedRef = useRef(false);

  useEffect(() => {
    unmountedRef.current = false;

    function invalidate(keys: unknown[][]) {
      for (const queryKey of keys) {
        queryClient.invalidateQueries({ queryKey });
      }
    }

    function handleMessage(event: MessageEvent) {
      let parsed: LiveEvent;
      try {
        parsed = JSON.parse(event.data as string) as LiveEvent;
      } catch {
        return;
      }

      setLastEvent(parsed);

      const { kind, payload_json } = parsed;
      const sid = strategyIdRef.current;

      if (kind === "engine.started" || kind === "engine.stopping") {
        invalidate([["events"], ["events-header"], ["strategies"]]);
        return;
      }

      if (kind === "position.opened" || kind === "position.closed") {
        invalidate([
          ["farb-positions-open", sid],
          ["farb-positions-active", sid],
          ["events"],
          ["equity", sid],
        ]);
        return;
      }

      if (kind === "tick.completed") {
        // Every minute tick produces a row in the events table (engine publishes
        // tick.completed → EventDbSink writes it). Invalidate the events queries
        // so RecentEvents and the Header pill pick up the new row.
        const keys: unknown[][] = [
          ["equity", sid],
          ["events"],
          ["events-header"],
        ];

        const openedCoins = (payload_json?.opened_coins as string[] | undefined) ?? [];
        const closedCoins = (payload_json?.closed_coins as string[] | undefined) ?? [];

        if (openedCoins.length > 0 || closedCoins.length > 0) {
          keys.push(["farb-positions-open", sid]);
          keys.push(["farb-positions-active", sid]);
        }

        invalidate(keys);
        return;
      }

      // Unknown kind — no invalidation needed
    }

    function connect() {
      if (unmountedRef.current) return;

      const url = `ws://${window.location.host}/ws/live`;
      const ws = new WebSocket(url);
      socketRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (unmountedRef.current) {
          ws.close();
          return;
        }
        backoffRef.current = 1_000; // reset backoff on successful connection
        setStatus("open");
      };

      ws.onmessage = handleMessage;

      ws.onclose = () => {
        if (unmountedRef.current) return;
        setStatus("closed");
        scheduleReconnect();
      };

      ws.onerror = () => {
        // onerror is always followed by onclose; let onclose handle reconnect
        if (unmountedRef.current) return;
        setStatus("closed");
      };
    }

    function scheduleReconnect() {
      if (unmountedRef.current) return;
      const delay = backoffRef.current;
      backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS);
      timerRef.current = setTimeout(connect, delay);
    }

    connect();

    return () => {
      unmountedRef.current = true;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (socketRef.current !== null) {
        socketRef.current.onclose = null; // prevent reconnect on intentional close
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [queryClient]);

  return { status, lastEvent };
}
