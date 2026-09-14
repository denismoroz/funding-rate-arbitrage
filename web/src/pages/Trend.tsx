import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { Header } from "../components/Header";
import {
  fetchTrendEquity,
  fetchTrendEvents,
  fetchTrendSummary,
  type TrendPosition,
  type TrendSummary,
} from "../lib/api";
import { formatCurrency } from "../lib/format";

const KIND_LABEL: Record<string, string> = {
  fund: "start: fund the account",
  open: "open",
  increase: "add",
  reduce: "trim",
  close: "close",
  flip: "flip side",
  liquidation: "LIQUIDATION",
};

function fmtTs(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", dateStyle: "short", timeStyle: "short" });
}

function signed(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = formatCurrency(Math.abs(n));
  return n >= 0 ? `+${abs}` : `−${abs}`;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-gray-100 bg-gray-50/60 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">{title}</div>
      {children}
    </div>
  );
}

function ResultPanel({ s }: { s: TrendSummary }) {
  const pos = (s.pnl ?? 0) >= 0;
  const aprReady = s.apr_pct != null && s.hours >= 24;
  return (
    <Panel title="Result">
      <div className="text-2xl font-semibold text-gray-900">{s.equity != null ? formatCurrency(s.equity) : "—"}</div>
      <div className="text-sm text-gray-500">started with {formatCurrency(s.capital)}</div>
      <div className={`mt-2 font-mono text-sm font-semibold ${pos ? "text-green-600" : "text-red-500"}`}>
        {s.pnl != null ? `${signed(s.pnl)} (${(s.pnl_pct ?? 0).toFixed(2)}%)` : "—"}
      </div>
      <div className="mt-1 text-sm text-gray-500" title="P&L since start, annualised linearly">
        APR{" "}
        {aprReady ? (
          <span className={`font-mono font-semibold ${(s.apr_pct ?? 0) >= 0 ? "text-green-600" : "text-red-500"}`}>
            {(s.apr_pct ?? 0) >= 0 ? "+" : ""}
            {(s.apr_pct ?? 0).toFixed(1)}%
          </span>
        ) : (
          <span className="text-gray-400">appears after 24h</span>
        )}
      </div>
      <div className="mt-2 text-xs text-gray-500">
        realised {signed(s.realized)} · funding {signed(s.funding_total)} · fees {formatCurrency(s.fees ?? 0)}
      </div>
    </Panel>
  );
}

function BookPanel({ s }: { s: TrendSummary }) {
  const longs = s.positions.filter((p) => p.units > 0);
  const shorts = s.positions.filter((p) => p.units < 0);
  const gross = s.gross_notional ?? 0;
  const longShare = gross > 0 ? (longs.reduce((a, p) => a + Math.abs(p.notional), 0) / gross) * 100 : 0;
  return (
    <Panel title="The book right now">
      <div className="flex h-4 w-full overflow-hidden rounded bg-gray-200">
        <div className="bg-green-500" style={{ width: `${longShare}%` }} title={`long ${longShare.toFixed(0)}%`} />
        <div className="bg-red-400" style={{ width: `${100 - longShare}%` }} title={`short ${(100 - longShare).toFixed(0)}%`} />
      </div>
      <div className="mt-2 space-y-1 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-500">{longs.length} long / {shorts.length} short</span>
          <span className="font-mono">{formatCurrency(gross)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500" title="sum of |position| over equity">gross leverage</span>
          <span className="font-mono">{s.gross_leverage != null ? `${s.gross_leverage.toFixed(2)}×` : "—"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500" title="long minus short: the book's directional tilt">net exposure</span>
          <span className={`font-mono ${(s.net_notional ?? 0) >= 0 ? "text-green-600" : "text-red-500"}`}>
            {signed(s.net_notional)}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">cash (USDC)</span>
          <span className="font-mono">{formatCurrency(s.cash ?? 0)}</span>
        </div>
      </div>
    </Panel>
  );
}

function RulePanel({ s }: { s: TrendSummary }) {
  return (
    <Panel title="Rule">
      <div className="space-y-1 text-sm">
        <div className="text-gray-600">
          long or short each coin by its own trend: the average sign of the return over{" "}
          {s.lookbacks.join(", ")} days
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">rebalance</span>
          <span className="font-mono">daily 00:00 UTC</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500" title="each position scaled to this daily volatility, then the book is capped and scaled down">
            size
          </span>
          <span className="font-mono">
            {(s.vol_target_daily * 100).toFixed(1)}%/day · cap {s.leverage_cap}× · {s.risk_scale}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500" title="the whole book is scaled every day so its own trailing 60-day volatility meets this target">
            book volatility target
          </span>
          <span className="font-mono">
            {s.book_vol_target_ann != null ? `${(s.book_vol_target_ann * 100).toFixed(0)}%/yr` : "off"}
            {s.size_scale != null && <span className="text-gray-400"> · now ×{s.size_scale.toFixed(2)}</span>}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">universe</span>
          <span className="font-mono">{s.universe.length} of {s.coins.length} coins</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">rebalances / fills</span>
          <span className="font-mono">{s.rebalances ?? 0} / {s.trades ?? 0}</span>
        </div>
        {(s.liquidations ?? 0) > 0 && <div className="text-red-500">liquidations: {s.liquidations}</div>}
      </div>
    </Panel>
  );
}

function PositionRow({ p, equity }: { p: TrendPosition; equity: number }) {
  const up = (p.unrealized ?? 0) >= 0;
  return (
    <tr className="border-t border-gray-100 font-mono text-sm">
      <td className="py-2 font-sans font-semibold">{p.coin}</td>
      <td className="py-2 font-sans">
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${
            p.side === "long" ? "bg-green-100 text-green-700" : p.side === "short" ? "bg-red-100 text-red-600" : "bg-gray-100 text-gray-500"
          }`}
        >
          {p.side}
        </span>
      </td>
      <td className="py-2" title="average sign of the trend over the lookbacks: +1 all agree up, −1 all agree down">
        {p.signal == null ? "—" : p.signal.toFixed(2)}
      </td>
      <td className="py-2">{(p.weight * 100).toFixed(1)}%</td>
      <td className="py-2">{formatCurrency(Math.abs(p.notional))}</td>
      <td className="py-2 text-gray-500">{equity > 0 ? `${((Math.abs(p.notional) / equity) * 100).toFixed(1)}%` : "—"}</td>
      <td className="py-2">{p.entry != null ? formatCurrency(p.entry) : "—"}</td>
      <td className="py-2">{p.price != null ? formatCurrency(p.price) : "—"}</td>
      <td className={`py-2 ${up ? "text-green-600" : "text-red-500"}`}>{signed(p.unrealized)}</td>
    </tr>
  );
}

export default function Trend() {
  const summary = useQuery({ queryKey: ["trend-summary"], queryFn: fetchTrendSummary, refetchInterval: 60_000 });
  const equity = useQuery({ queryKey: ["trend-equity"], queryFn: fetchTrendEquity, refetchInterval: 60_000 });
  const events = useQuery({ queryKey: ["trend-events"], queryFn: () => fetchTrendEvents(200), refetchInterval: 60_000 });

  const s = summary.data;
  const points = useMemo(
    () =>
      (equity.data ?? []).map((p) => ({
        label: new Date(p.ts_ms).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", month: "2-digit", day: "2-digit", hour: "2-digit" }),
        equity: p.equity,
      })),
    [equity.data],
  );

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus="closed" route="trend" />
      <main className="mx-auto max-w-6xl space-y-4 p-4">
        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between">
            <div>
              <h1 className="text-lg font-semibold text-gray-900">Trend · paper test</h1>
              <p className="text-sm text-gray-500">
                Long the coins that are trending up, short the ones trending down, on HL perps. Paper only — no orders.
              </p>
              <p className="mt-1 text-xs text-gray-400">
                Backtest 2020–2026 on HL-listed coins, sized to a 14%/yr book volatility: about +15%/yr with a 13%
                worst drawdown; uncorrelated with FRAB and with B v2 (research/trend_following/FINDINGS.md,
                risk_shaping.py).
              </p>
            </div>
            <div className="text-right text-xs text-gray-400">
              <div>last tick {fmtTs(s?.last_tick_ms)}</div>
              <div>last rebalance {fmtTs(s?.last_rebalance_ms)}</div>
              {s?.last_error && <div className="text-red-500">{s.last_error}</div>}
            </div>
          </div>

          {s && s.started ? (
            <div className="mt-3 grid gap-3 md:grid-cols-3">
              <ResultPanel s={s} />
              <BookPanel s={s} />
              <RulePanel s={s} />
            </div>
          ) : (
            <div className="mt-3 text-sm text-gray-400">waiting for the first closed hour…</div>
          )}

          <div className="mt-3 h-64">
            {points.length > 1 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={points}>
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} minTickGap={40} />
                  <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11 }} width={60} tickFormatter={(v) => `$${Number(v).toFixed(2)}`} />
                  <Tooltip formatter={(v: number) => formatCurrency(v)} />
                  {s && <ReferenceLine y={s.capital} stroke="#9ca3af" strokeDasharray="4 4" />}
                  <Line type="monotone" dataKey="equity" stroke="#2563eb" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-gray-400">
                equity curve appears after the second closed hour
              </div>
            )}
          </div>
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h2 className="mb-2 text-sm font-semibold text-gray-700">Positions</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-400">
                <tr>
                  <th className="py-1">coin</th>
                  <th>side</th>
                  <th title="average sign of the trend over the lookbacks">signal</th>
                  <th title="target share of equity">target</th>
                  <th>notional</th>
                  <th>of equity</th>
                  <th>entry</th>
                  <th>price</th>
                  <th>open P&L</th>
                </tr>
              </thead>
              <tbody>
                {(s?.positions ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-2 text-gray-400">no positions yet</td>
                  </tr>
                ) : (
                  (s?.positions ?? []).map((p) => <PositionRow key={p.coin} p={p} equity={s?.equity ?? 0} />)
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h2 className="mb-2 text-sm font-semibold text-gray-700">Paper fills</h2>
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-white text-xs uppercase text-gray-400">
                <tr>
                  <th className="py-1">time (MSK)</th><th>coin</th><th>action</th><th>notional</th><th>price</th><th>fee</th>
                </tr>
              </thead>
              <tbody>
                {(events.data ?? []).map((e, i) => (
                  <tr key={i} className="border-t border-gray-100">
                    <td className="py-1.5 text-gray-500">{fmtTs(e.ts_ms)}</td>
                    <td className="font-semibold">{e.coin}</td>
                    <td>{KIND_LABEL[e.kind] ?? e.kind}</td>
                    <td className="font-mono">{formatCurrency(e.notional)}</td>
                    <td className="font-mono">{formatCurrency(e.price)}</td>
                    <td className="font-mono text-gray-500">{formatCurrency(e.fee)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  );
}
