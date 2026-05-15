const BASE = "/api";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function apiPostJson<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg: string;
    try {
      const json = (await res.json()) as unknown;
      msg = typeof json === "object" && json !== null && "detail" in json
        ? String((json as Record<string, unknown>).detail)
        : JSON.stringify(json);
    } catch {
      msg = res.statusText;
    }
    throw new Error(`${res.status}: ${msg}`);
  }
  return res.json() as Promise<TRes>;
}

// ── Types ─────────────────────────────────────────────────────────────────────

export type StrategyParams = {
  coins: string[];
  entry_threshold: number;
  exit_threshold: number;
  min_hold_hours: number;
  signal_window_hours: number;
  concurrency_cap: number;
  position_size_usdc: number;
};

export type StrategyParamsHot = Pick<
  StrategyParams,
  "entry_threshold" | "exit_threshold" | "min_hold_hours" | "concurrency_cap" | "position_size_usdc"
>;

export type Strategy = {
  id: number;
  name: string;
  version: string;
  params_json: Record<string, unknown>;
  status: string;
  started_at: string | null;
  stopped_at: string | null;
};

export type EquitySnapshot = {
  id: number;
  strategy_id: number;
  ts: string;
  total_equity: number;
  cash: number;
  spot_value: number;
  perp_unrealized: number;
  perp_realized_cum: number;
  funding_cum: number;
  fees_cum: number;
};

export type Fill = {
  id: number;
  position_id: number;
  ts: string;
  leg: "SPOT" | "PERP";
  side: "BUY" | "SELL";
  qty: number;
  price: number;
  fee: number;
  slippage_bps: number;
  is_paper: boolean;
};

export type Position = {
  id: number;
  strategy_id: number;
  market_id: number;
  coin: string;
  mode: string;
  status: "OPEN" | "CLOSED";
  opened_at: string;
  closed_at: string | null;
  spot_units: number;
  perp_units: number;
  entry_spot_price: number;
  entry_perp_price: number;
  exit_spot_price: number | null;
  exit_perp_price: number | null;
  realized_pnl: number;
  funding_collected: number;
  fees_paid: number;
  fills: Fill[];
};

export type Signal = {
  id: number;
  strategy_id: number;
  market_id: number;
  coin: string;
  ts: string;
  signal_value: number;
  regime_pass: boolean;
  action: "NONE" | "OPEN" | "CLOSE";
};

export type Event = {
  id: number;
  ts: string;
  level: string;
  source: string;
  kind: string;
  message: string;
  payload_json: Record<string, unknown> | null;
};

// ── Fetch helpers ─────────────────────────────────────────────────────────────

export function fetchStrategies(): Promise<Strategy[]> {
  return apiFetch<Strategy[]>("/strategies");
}

export function fetchEquity(
  strategyId: number,
  opts?: { limit?: number; since?: string },
): Promise<EquitySnapshot[]> {
  const params = new URLSearchParams({ strategy_id: String(strategyId) });
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.since != null) params.set("since", opts.since);
  return apiFetch<EquitySnapshot[]>(`/equity?${params}`);
}

export function fetchPositions(opts?: {
  strategyId?: number;
  status?: string;
  limit?: number;
}): Promise<Position[]> {
  const params = new URLSearchParams();
  if (opts?.strategyId != null)
    params.set("strategy_id", String(opts.strategyId));
  if (opts?.status != null) params.set("status", opts.status);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<Position[]>(`/positions?${params}`);
}

export function fetchSignals(opts?: {
  strategyId?: number;
  limit?: number;
}): Promise<Signal[]> {
  const params = new URLSearchParams();
  if (opts?.strategyId != null)
    params.set("strategy_id", String(opts.strategyId));
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<Signal[]>(`/signals?${params}`);
}

export function fetchEvents(opts?: { limit?: number }): Promise<Event[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<Event[]>(`/events?${params}`);
}

export function fetchStrategyParams(id: number): Promise<StrategyParams> {
  return apiFetch<StrategyParams>(`/strategies/${id}/params`);
}

export function deployStrategyParams(id: number, body: StrategyParamsHot): Promise<StrategyParams> {
  return apiPostJson<StrategyParamsHot, StrategyParams>(`/strategies/${id}/deploy`, body);
}
