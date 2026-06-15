import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategies,
  fetchEvents,
  tsMsToDate,
} from "../lib/api";
import { formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { type WsStatus } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { pauseStrategy, resumeStrategy } from "../lib/api";

const WS_DOT: Record<WsStatus, string> = {
  open: "bg-green-500",
  connecting: "bg-yellow-500",
  closed: "bg-red-500",
};

export function Header({ wsStatus, route }: { wsStatus: WsStatus; route: "dashboard" | "settings" | "funding" | "journal" | "xsmom" }) {
  const now = useNow();
  const strategyId = useActiveStrategyId();
  const queryClient = useQueryClient();
  const stratQ = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    staleTime: 60_000,
  });

  const eventsQ = useQuery({
    queryKey: ["events-header"],
    queryFn: () => fetchEvents({ limit: 20 }),
  });

  const strategy = stratQ.data?.find((s) => s.id === strategyId);

  const toggleMutation = useMutation({
    mutationFn: () => {
      if (!strategyId) throw new Error("No strategy");
      return strategy?.status === "paused"
        ? resumeStrategy(strategyId)
        : pauseStrategy(strategyId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (err: Error) => {
      alert(err.message);
    },
  });

  const lastTick = eventsQ.data?.find((e) => e.kind === "tick.completed");
  // WS events still carry `ts` as ISO string; DB events have ts_ms
  const lastTickAgeMs = lastTick
    ? now - tsMsToDate(lastTick.ts_ms).getTime()
    : Infinity;
  const engineAlive = lastTickAgeMs <= 90_000;
  const engineEvent = engineAlive
    ? lastTick
    : eventsQ.data?.find((e) => e.kind.startsWith("engine."));
  const engineLabel = engineAlive ? "running" : engineEvent?.message;

  return (
    <header className="flex items-center gap-4 border-b border-gray-200 bg-gray-900 px-6 py-3">
      <span className="text-lg font-bold tracking-tight text-white">frab</span>

      <nav className="flex items-center gap-3 text-sm">
        <a
          href="#/"
          className={route === "dashboard" ? "text-white font-medium" : "text-gray-400 hover:text-gray-200"}
        >
          FRAB
        </a>
        <a
          href="#/xsmom"
          className={route === "xsmom" ? "text-white font-medium" : "text-gray-400 hover:text-gray-200"}
        >
          XSMOM
        </a>
        <span className="text-gray-600">·</span>
        <a
          href="#/settings"
          className={route === "settings" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Settings
        </a>
        <a
          href="#/funding"
          className={route === "funding" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Funding
        </a>
        <a
          href="#/journal"
          className={route === "journal" ? "text-white" : "text-gray-400 hover:text-gray-200"}
        >
          Journal
        </a>
      </nav>

      {strategy && route !== "xsmom" && (
        <span className="inline-flex items-center gap-2 text-xs font-medium text-white">
          <span className="text-gray-200">{strategy.name} {strategy.version}</span>
          <span className="text-gray-500">·</span>
          <button
            type="button"
            role="switch"
            aria-checked={strategy.status === "active"}
            disabled={toggleMutation.isPending}
            onClick={() => toggleMutation.mutate()}
            title={strategy.status === "paused" ? "Resume strategy" : "Pause strategy"}
            className={[
              "relative inline-flex h-[22px] w-10 shrink-0 cursor-pointer rounded-full border-2 border-transparent",
              "transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/75",
              strategy.status === "active" ? "bg-emerald-500" : "bg-slate-600",
              toggleMutation.isPending ? "opacity-50 cursor-not-allowed" : "",
            ].join(" ")}
          >
            <span
              className={[
                "pointer-events-none inline-block h-[18px] w-[18px] rounded-full bg-white shadow-sm",
                "transform transition-transform duration-200",
                strategy.status === "active" ? "translate-x-[18px]" : "translate-x-0",
              ].join(" ")}
            />
          </button>
        </span>
      )}

      {engineEvent && (
        <span className="ml-auto flex items-center gap-1.5 text-xs text-gray-400">
          engine:{" "}
          <span className="text-gray-200">{engineLabel}</span>
          <span className="text-gray-500">
            ({formatRelative(engineEvent.ts_ms, now)})
          </span>
          <span
            className={`inline-block h-2 w-2 rounded-full ${WS_DOT[wsStatus]}`}
            title={wsStatus}
          />
        </span>
      )}

      {!engineEvent && (
        <span className="ml-auto flex items-center gap-1.5 text-xs text-gray-400">
          <span
            className={`inline-block h-2 w-2 rounded-full ${WS_DOT[wsStatus]}`}
            title={wsStatus}
          />
        </span>
      )}
    </header>
  );
}
