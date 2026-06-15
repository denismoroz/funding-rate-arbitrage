import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategies,
  fetchEvents,
  tsMsToDate,
  pauseStrategy,
  resumeStrategy,
} from "../lib/api";
import { formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { type WsStatus } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { useToggleXsmom } from "../lib/useXsmom";

export type Route =
  | "dashboard"
  | "settings"
  | "funding"
  | "journal"
  | "xsmom"
  | "xsmom-journal"
  | "xsmom-settings";

type Section = "frab" | "xsmom";

const WS_DOT: Record<WsStatus, string> = {
  open: "bg-green-500",
  connecting: "bg-yellow-500",
  closed: "bg-red-500",
};

function sectionOf(route: Route): Section {
  return route.startsWith("xsmom") ? "xsmom" : "frab";
}

type SubTab = { label: string; href: string; match: Route };

const FRAB_TABS: SubTab[] = [
  { label: "Overview", href: "#/", match: "dashboard" },
  { label: "Settings", href: "#/settings", match: "settings" },
  { label: "Funding", href: "#/funding", match: "funding" },
  { label: "Journal", href: "#/journal", match: "journal" },
];

const XSMOM_TABS: SubTab[] = [
  { label: "Overview", href: "#/xsmom", match: "xsmom" },
  { label: "Journal", href: "#/xsmom/journal", match: "xsmom-journal" },
  { label: "Settings", href: "#/xsmom/settings", match: "xsmom-settings" },
];

// ── presentational on/off switch ──────────────────────────────────────────────

function ToggleSwitch({
  label,
  status,
  pending,
  onToggle,
}: {
  label: string;
  status: string;
  pending: boolean;
  onToggle: () => void;
}) {
  const active = status === "active";
  return (
    <span className="inline-flex items-center gap-2 text-xs font-medium text-white">
      <span className="text-gray-200">{label}</span>
      <span className="text-gray-500">·</span>
      <span className="text-gray-400">{active ? "running" : status}</span>
      <button
        type="button"
        role="switch"
        aria-checked={active}
        disabled={pending}
        onClick={onToggle}
        title={active ? "Pause strategy" : "Resume strategy"}
        className={[
          "relative inline-flex h-[22px] w-10 shrink-0 cursor-pointer rounded-full border-2 border-transparent",
          "transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/75",
          active ? "bg-emerald-500" : "bg-slate-600",
          pending ? "opacity-50 cursor-not-allowed" : "",
        ].join(" ")}
      >
        <span
          className={[
            "pointer-events-none inline-block h-[18px] w-[18px] rounded-full bg-white shadow-sm",
            "transform transition-transform duration-200",
            active ? "translate-x-[18px]" : "translate-x-0",
          ].join(" ")}
        />
      </button>
    </span>
  );
}

// ── per-section toggles ────────────────────────────────────────────────────────

function FrabToggle() {
  const strategyId = useActiveStrategyId();
  const queryClient = useQueryClient();
  const stratQ = useQuery({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
    staleTime: 60_000,
  });
  const strategy = stratQ.data?.find((s) => s.id === strategyId);

  const toggle = useMutation({
    mutationFn: () => {
      if (!strategyId) throw new Error("No strategy");
      return strategy?.status === "paused"
        ? resumeStrategy(strategyId)
        : pauseStrategy(strategyId);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["strategies"] }),
    onError: (err: Error) => alert(err.message),
  });

  if (!strategy) return null;
  return (
    <ToggleSwitch
      label={`${strategy.name} ${strategy.version}`}
      status={strategy.status}
      pending={toggle.isPending}
      onToggle={() => toggle.mutate()}
    />
  );
}

function XsmomToggle() {
  const { mutation, strategy } = useToggleXsmom();
  if (!strategy) {
    return (
      <span className="text-xs italic text-gray-400">XSMOM strategy not found</span>
    );
  }
  return (
    <ToggleSwitch
      label={`${strategy.name} ${strategy.version}`}
      status={strategy.status}
      pending={mutation.isPending}
      onToggle={() => mutation.mutate()}
    />
  );
}

// ── header ─────────────────────────────────────────────────────────────────────

export function Header({ wsStatus, route }: { wsStatus: WsStatus; route: Route }) {
  const now = useNow();
  const section = sectionOf(route);

  const eventsQ = useQuery({
    queryKey: ["events-header"],
    queryFn: () => fetchEvents({ limit: 20 }),
  });

  const lastTick = eventsQ.data?.find((e) => e.kind === "tick.completed");
  const lastTickAgeMs = lastTick
    ? now - tsMsToDate(lastTick.ts_ms).getTime()
    : Infinity;
  const engineAlive = lastTickAgeMs <= 90_000;
  const engineEvent = engineAlive
    ? lastTick
    : eventsQ.data?.find((e) => e.kind.startsWith("engine."));
  const engineLabel = engineAlive ? "running" : engineEvent?.message;

  const tabs = section === "xsmom" ? XSMOM_TABS : FRAB_TABS;

  const sectionBtn = (label: string, href: string, active: boolean) => (
    <a
      href={href}
      className={[
        "rounded px-3 py-1 text-sm font-semibold transition-colors",
        active
          ? "bg-white text-gray-900"
          : "text-gray-400 hover:bg-gray-800 hover:text-gray-200",
      ].join(" ")}
    >
      {label}
    </a>
  );

  return (
    <header className="border-b border-gray-200">
      {/* Level 1 — sections */}
      <div className="flex items-center gap-4 bg-gray-900 px-6 py-2.5">
        <span className="text-lg font-bold tracking-tight text-white">frab</span>
        <nav className="flex items-center gap-2">
          {sectionBtn("FRAB", "#/", section === "frab")}
          {sectionBtn("XSMOM", "#/xsmom", section === "xsmom")}
        </nav>

        <span className="ml-auto flex items-center gap-1.5 text-xs text-gray-400">
          {engineEvent && (
            <>
              engine: <span className="text-gray-200">{engineLabel}</span>
              <span className="text-gray-500">
                ({formatRelative(engineEvent.ts_ms, now)})
              </span>
            </>
          )}
          <span
            className={`inline-block h-2 w-2 rounded-full ${WS_DOT[wsStatus]}`}
            title={wsStatus}
          />
        </span>
      </div>

      {/* Level 2 — sub-nav for the active section + on/off toggle */}
      <div className="flex items-center gap-1 bg-gray-800 px-6 py-1.5">
        <nav className="flex items-center gap-1 text-sm">
          {tabs.map((t) => (
            <a
              key={t.match}
              href={t.href}
              className={[
                "rounded px-2.5 py-1 transition-colors",
                route === t.match
                  ? "bg-gray-700 text-white"
                  : "text-gray-400 hover:text-gray-200",
              ].join(" ")}
            >
              {t.label}
            </a>
          ))}
        </nav>
        <span className="ml-auto">
          {section === "xsmom" ? <XsmomToggle /> : <FrabToggle />}
        </span>
      </div>
    </header>
  );
}
