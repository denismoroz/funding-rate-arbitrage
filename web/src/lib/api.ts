const BASE = "/api";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function apiPatchJson<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
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

async function apiPost<TRes>(path: string): Promise<TRes> {
  const res = await fetch(`${BASE}${path}`, { method: "POST" });
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

// Convert epoch ms (integer) to Date — backend sends ts_ms not ts.
export function tsMsToDate(ms: number): Date {
  return new Date(ms);
}

// ── Types ─────────────────────────────────────────────────────────────────────

export type Strategy = {
  id: number;
  name: string;
  version: string;
  params_json: Record<string, unknown>;
  status: string;
  started_at_ms: number | null;
  stopped_at_ms: number | null;
};

export type EquitySnapshot = {
  id: number;
  strategy_id: number;
  ts_ms: number;
  total_equity: number;
  cash: number;
  spot_value: number;
  perp_unrealized: number;
  perp_realized_cum: number;
  funding_cum: number;
  fees_cum: number;
};

export type Leg = {
  id: number;
  qty: number;
  entry_price: number;
} | null;

export type FarbPosition = {
  id: number;
  strategy_id: number;
  coin: string;
  state: string;
  state_data: Record<string, unknown>;
  opened_at_ms: number;
  closed_at_ms: number | null;
  legs: { collateral: Leg; spot: Leg; perp: Leg };
  hours_held: number | null;
  target_signal_apr: number | null;
  exit_signal_apr: number | null;
  current_signal_apr: number | null;
  consec_negative_hours: number | null;
  unrealized_pnl_usdc: number | null;
  funding_usdc: number;
  fees_usdc: number;
  breakeven_hours_remaining: number | null;
  locked_margin_usdc: number;
  leverage: number | null;
  capital_usdc: number;
};

export type FundingRate = {
  id: number;
  exchange_id: number;
  coin: string;
  ts_ms: number;
  rate: number;
  premium: number | null;
  annualized_pct: number;
};

export type Event = {
  id: number;
  ts_ms: number;
  level: string;
  source: string;
  kind: string;
  message: string;
  payload_json: Record<string, unknown> | null;
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

export type StrategyParamsPatch = {
  params: Record<string, number | string | boolean | string[] | null>;
};

export type StrategyParamsPatchResponse = {
  id: number;
  params_json: Record<string, unknown>;
  restart_required: boolean;
  note?: string;
};

// ── Fetchers ──────────────────────────────────────────────────────────────────

export function fetchStrategies(): Promise<Strategy[]> {
  return apiFetch<Strategy[]>("/strategies");
}

export function fetchStrategy(id: number): Promise<Strategy> {
  return apiFetch<Strategy>(`/strategies/${id}`);
}

export function fetchEquity(
  strategyId: number,
  opts?: { limit?: number },
): Promise<EquitySnapshot[]> {
  const params = new URLSearchParams({ strategy_id: String(strategyId) });
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<EquitySnapshot[]>(`/equity?${params}`);
}

export type EquitySummary = {
  ts_ms: number;
  total: number;
  long: number;
  short: number;
  free: number;
  locked: number;
  reserved: number;
};

export function fetchEquitySummary(): Promise<EquitySummary> {
  return apiFetch<EquitySummary>("/equity/summary");
}

export function fetchFarbPositions(
  strategyId: number,
  status?: string,
): Promise<FarbPosition[]> {
  const params = new URLSearchParams({ strategy_id: String(strategyId) });
  if (status != null) params.set("status", status);
  return apiFetch<FarbPosition[]>(`/farb-positions?${params}`);
}

export function fetchFarbPosition(id: number): Promise<FarbPosition> {
  return apiFetch<FarbPosition>(`/farb-positions/${id}`);
}

export function fetchFundingHistory(
  coin: string,
  opts?: { limit?: number; exchangeId?: number },
): Promise<FundingRate[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.exchangeId != null) params.set("exchange_id", String(opts.exchangeId));
  const qs = params.toString();
  return apiFetch<FundingRate[]>(`/funding/${coin}${qs ? `?${qs}` : ""}`);
}

export function fetchEvents(opts?: { limit?: number; level?: string }): Promise<Event[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.level != null) params.set("level", opts.level);
  return apiFetch<Event[]>(`/events?${params}`);
}

export function fetchAlerts(opts: { strategyId: number }): Promise<Alert[]> {
  const params = new URLSearchParams({ strategy_id: String(opts.strategyId) });
  return apiFetch<Alert[]>(`/alerts?${params}`);
}

export function patchStrategyParams(
  id: number,
  patch: StrategyParamsPatch,
): Promise<StrategyParamsPatchResponse> {
  return apiPatchJson<StrategyParamsPatch, StrategyParamsPatchResponse>(
    `/strategies/${id}/params`,
    patch,
  );
}

// Alias for StrategyParamsPatch value type — exported for use in consumers.
export type ParamValue = number | string | boolean | string[] | null;

export type ForceTickResponse = {
  status: string;
  ts_ms: number;
  message: string;
};

export function forceHourTick(id: number): Promise<ForceTickResponse> {
  return apiPost<ForceTickResponse>(`/strategies/${id}/force-tick`);
}
