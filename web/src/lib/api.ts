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

export interface HotFieldSpec {
  type: "float" | "int";
  label: string;
  min_value: number | null;
  max_value: number | null;
  exclusive_min: boolean;
  exclusive_max: boolean;
  description: string;
}

export interface StrategyParamsResponse {
  strategy_name: string;
  version: string;
  params: Record<string, unknown>;
  hot_schema: Record<string, HotFieldSpec>;
}

export interface DeployResponse {
  strategy_name: string;
  version: string;
  params: Record<string, unknown>;
}

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
  // MTM fields (may be null if price unavailable):
  current_mark: number | null;
  spot_value_now: number | null;
  perp_unrealized: number | null;
  notional_at_entry: number | null;
  net_mtm: number | null;
  // Cost/projection fields:
  slippage_cost: number | null;
  breakeven_at: string | null;     // ISO datetime or null
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

export type FundingRate = {
  id: number;
  market_id: number;
  coin: string;
  ts: string;
  rate: number;
  premium: number | null;
  annualized_pct: number;
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

export type PositionFundingAccrual = {
  id: number;
  position_id: number;
  ts: string;
  delta: number;
};

export type Alert = {
  type: "failed_position" | "event";
  severity: "WARNING" | "ERROR";
  ts: string;
  coin: string | null;
  message: string;
  position_id: number | null;
  payload: Record<string, unknown> | null;
};

export type SpotBalanceItem = {
  coin: string;
  qty: number;
  mark: number;
  usd_value: number;
};

export type WalletBalance = {
  perp_account_value: number;
  perp_unrealized_pnl: number;
  spot_balances: SpotBalanceItem[];
  usdc_spot: number;
  total_usd: number;
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

export function fetchFundingHistory(
  coin: string,
  opts?: { limit?: number; since?: string },
): Promise<FundingRate[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.since != null) params.set("since", opts.since);
  const qs = params.toString();
  return apiFetch<FundingRate[]>(`/funding/${coin}${qs ? `?${qs}` : ""}`);
}

export function fetchEvents(opts?: { limit?: number }): Promise<Event[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<Event[]>(`/events?${params}`);
}

export function fetchAlerts(opts: {
  strategyId: number;
  since?: string;
}): Promise<Alert[]> {
  const params = new URLSearchParams({ strategy_id: String(opts.strategyId) });
  if (opts.since != null) params.set("since", opts.since);
  return apiFetch<Alert[]>(`/alerts?${params}`);
}

export function fetchPositionFundingHistory(
  positionId: number,
  opts?: { limit?: number },
): Promise<PositionFundingAccrual[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return apiFetch<PositionFundingAccrual[]>(
    `/positions/${positionId}/funding-history${qs ? `?${qs}` : ""}`,
  );
}

export function fetchWallet(strategyId: number): Promise<WalletBalance> {
  const params = new URLSearchParams({ strategy_id: String(strategyId) });
  return apiFetch<WalletBalance>(`/equity/wallet?${params}`);
}

export function fetchStrategyParams(id: number): Promise<StrategyParamsResponse> {
  return apiFetch<StrategyParamsResponse>(`/strategies/${id}/params`);
}

export function deployStrategyParams(
  id: number,
  body: Record<string, number>,
): Promise<DeployResponse> {
  return apiPostJson<Record<string, number>, DeployResponse>(`/strategies/${id}/deploy`, body);
}

export function forceHourTick(id: number): Promise<{ status: string; message: string }> {
  return apiPostJson<Record<string, never>, { status: string; message: string }>(
    `/strategies/${id}/force-tick`,
    {},
  );
}
