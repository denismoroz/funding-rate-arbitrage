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

// ── State helpers ─────────────────────────────────────────────────────────────

/** FarbState values as returned by the API (UPPERCASE enum names). */
export const FARB_STATE = {
  PRE_BREAKEVEN: "PRE_BREAKEVEN",
  POST_BREAKEVEN: "POST_BREAKEVEN",
  CLOSED: "CLOSED",
  FAILED: "FAILED",
  // Transient (mid open/close)
  CHECK_MARGIN: "CHECK_MARGIN",
  OPENING_MARGIN: "OPENING_MARGIN",
  OPENING_LONG: "OPENING_LONG",
  OPENING_SHORT: "OPENING_SHORT",
  CLOSING_SHORT: "CLOSING_SHORT",
  CLOSING_LONG: "CLOSING_LONG",
  RELEASING_MARGIN: "RELEASING_MARGIN",
} as const;

/** A position is "active" (holding legs on HL) when in one of these two states. */
export function isActiveState(state: string): boolean {
  return state === FARB_STATE.PRE_BREAKEVEN || state === FARB_STATE.POST_BREAKEVEN;
}

/** Map a raw FarbState string to a human-readable label. */
export function farbStateLabel(state: string): string {
  switch (state) {
    case "PRE_BREAKEVEN": return "pre-break-even";
    case "POST_BREAKEVEN": return "post-break-even";
    case "CLOSED": return "closed";
    case "FAILED": return "failed";
    case "CHECK_MARGIN": return "check margin";
    case "OPENING_MARGIN": return "opening margin";
    case "OPENING_LONG": return "opening long";
    case "OPENING_SHORT": return "opening short";
    case "CLOSING_SHORT": return "closing short";
    case "CLOSING_LONG": return "closing long";
    case "RELEASING_MARGIN": return "releasing margin";
    default: return state.toLowerCase().replace(/_/g, " ");
  }
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
  spot_unrealized_pnl_usdc: number | null;
  perp_unrealized_pnl_usdc: number | null;
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

export type MarginStatus = "healthy" | "warning" | "forced_close" | "liquidation_imminent";

export type MarginFpAssessment = {
  farb_position_id: number;
  coin: string;
  virtual_ratio: number;
  status: MarginStatus;
  virtual_equity_usdc: number;
  virtual_maintenance_usdc: number;
};

export type MarginState = {
  ts_ms: number;
  account: {
    ratio: number;
    status: MarginStatus;
    equity_usdc: number;
    total_maintenance_usdc: number;
  };
  thresholds: { healthy: number; forced_close: number; liquidation: number };
  per_fp: MarginFpAssessment[];
  weakest_fp_id: number | null;
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

export function fetchMarginState(): Promise<MarginState> {
  return apiFetch<MarginState>("/equity/margin");
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

export function fetchEvents(opts?: { limit?: number; level?: string; kindPrefix?: string }): Promise<Event[]> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.level != null) params.set("level", opts.level);
  if (opts?.kindPrefix != null) params.set("kind_prefix", opts.kindPrefix);
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

export type StrategyStatusResponse = { id: number; status: string; ts_ms: number };
export function pauseStrategy(id: number): Promise<StrategyStatusResponse> {
  return apiPost<StrategyStatusResponse>(`/strategies/${id}/pause`);
}
export function resumeStrategy(id: number): Promise<StrategyStatusResponse> {
  return apiPost<StrategyStatusResponse>(`/strategies/${id}/resume`);
}

export type CloseFpResponse = { id: number; coin: string; new_state: string; ts_ms: number };
export function closeFarbPosition(id: number): Promise<CloseFpResponse> {
  return apiPost<CloseFpResponse>(`/farb-positions/${id}/close`);
}

export type CloseAllResponse = {
  closed_ids: number[];
  failed: { id: number; coin: string; reason: string }[];
  ts_ms: number;
};
export function closeAllFarbPositions(strategyId: number): Promise<CloseAllResponse> {
  return apiPost<CloseAllResponse>(`/farb-positions/close-all?strategy_id=${strategyId}`);
}

export type ManualOpenResponse = {
  id: number;
  coin: string;
  state: string;
  ts_ms: number;
};

export async function manualOpenFarbPosition(coin: string): Promise<ManualOpenResponse> {
  const r = await fetch(`${BASE}/farb-positions/manual-open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ coin }),
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`manual-open failed (${r.status}): ${detail}`);
  }
  return r.json();
}

// ── XSMOM Types ───────────────────────────────────────────────────────────────

export type XsmomSummary = {
  cash: number;
  long_total: number;
  short_total: number;
  pnl_total: number | null;
  n_long: number;
  n_short: number;
};

export type XsmomPerpLeg = {
  id: number;
  qty: number;
  entry_price: number;
};

export type XsmomPosition = {
  id: number;
  strategy_id: number;
  coin: string;
  side: "long" | "short";
  state: "NEW" | "OPENED" | "CLOSE" | "CLOSED" | "FAILED";
  state_data: Record<string, unknown>;
  score: number | null;
  perp_leg: XsmomPerpLeg | null;
  hours_held: number | null;
  unrealized_pnl_usdc: number | null;
  funding_usdc: number;
  fees_usdc: number;
  notional: number;
  required_margin: number;
  locked_margin_usdc: number;
  leverage: number | null;
  opened_at_ms: number;
  closed_at_ms: number | null;
};

export type XsmomScanRankItem = {
  coin: string;
  score: number;
  rank: number;
  leg: "long" | "short" | null;
};

export type XsmomScan = {
  id: number;
  strategy_id: number;
  ts_ms: number;
  ranking: XsmomScanRankItem[];
  n_long: number;
  n_short: number;
  note: string | null;
};

export type XsmomParamsResponse = {
  params: Record<string, unknown>;
  universe: string[];
};

export type XsmomClosePositionResponse = {
  id: number;
  coin: string;
  new_state: string;
  ts_ms: number;
};

export type XsmomCloseAllResponse = {
  closed: { id: number; coin: string }[];
  ts_ms: number;
};

export type XsmomRebalanceResponse = {
  kept: string[];
  opened: number[];
  dropped: number[];
  flipped: string[];
  ts_ms: number;
};

export type XsmomParamsPatchResponse = {
  params_json: Record<string, unknown>;
  restart_required: boolean;
};

// ── XSMOM State helpers ───────────────────────────────────────────────────────

/** Map a raw XsmomPosition state to a human-readable label. */
export function xsmomStateLabel(state: string): string {
  switch (state) {
    case "NEW": return "new";
    case "OPENED": return "opened";
    case "CLOSE": return "closing";
    case "CLOSED": return "closed";
    case "FAILED": return "failed";
    default: return state.toLowerCase().replace(/_/g, " ");
  }
}

/** A position is "open" (holding legs on HL) when in OPENED state. */
export function isXsmomOpen(state: string): boolean {
  return state === "OPENED";
}

// ── XSMOM Fetchers ────────────────────────────────────────────────────────────

export function fetchXsmomSummary(): Promise<XsmomSummary> {
  return apiFetch<XsmomSummary>("/xsmom/summary");
}

export function fetchXsmomPositions(status?: string): Promise<XsmomPosition[]> {
  const params = new URLSearchParams();
  if (status != null) params.set("status", status);
  const qs = params.toString();
  return apiFetch<XsmomPosition[]>(`/xsmom/positions${qs ? `?${qs}` : ""}`);
}

export function closeXsmomPosition(id: number): Promise<XsmomClosePositionResponse> {
  return apiPost<XsmomClosePositionResponse>(`/xsmom/positions/${id}/close`);
}

export function closeAllXsmomPositions(): Promise<XsmomCloseAllResponse> {
  return apiPost<XsmomCloseAllResponse>("/xsmom/positions/close-all");
}

export function rebalanceXsmom(): Promise<XsmomRebalanceResponse> {
  return apiPost<XsmomRebalanceResponse>("/xsmom/rebalance");
}

export function fetchXsmomScans(limit = 50): Promise<XsmomScan[]> {
  return apiFetch<XsmomScan[]>(`/xsmom/scans?limit=${limit}`);
}

export function fetchXsmomParams(): Promise<XsmomParamsResponse> {
  return apiFetch<XsmomParamsResponse>("/xsmom/params");
}

export function patchXsmomParams(
  patch: { params: Record<string, unknown> },
): Promise<XsmomParamsPatchResponse> {
  return apiPatchJson<{ params: Record<string, unknown> }, XsmomParamsPatchResponse>(
    "/xsmom/params",
    patch,
  );
}

export function resetXsmomEquity(): Promise<{ equity_baseline_ms: number }> {
  return apiPost<{ equity_baseline_ms: number }>("/xsmom/equity/reset");
}
