import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { Header } from "../components/Header";
import { fetchB2Events, fetchB2Equity, fetchB2Summary, type B2Coin } from "../lib/api";
import { formatCurrency } from "../lib/format";

const KIND_LABEL: Record<string, string> = {
  init_spot_buy: "start: buy spot",
  hedge_open: "hedge ON (short perp)",
  hedge_close: "hedge OFF",
  refill_buy: "refill spot",
  ratchet_sell: "ratchet: sell spot",
  carry_open: "carry ON",
  carry_close: "carry OFF",
  carry_reduce: "carry cut: margin for hedge",
  margin_rebalance: "margin top-up: sell spot + cut short",
  liquidation: "LIQUIDATION",
};

function lev(x: number | null | undefined): string {
  return x == null ? "—" : `${Number(x.toFixed(2))}×`;
}

function fmtTs(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", dateStyle: "short", timeStyle: "short" });
}

function signed(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = formatCurrency(Math.abs(n));
  return n >= 0 ? `+${abs}` : `−${abs}`;
}

function Pill({ on, label }: { on: boolean | undefined; label: string }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${on ? "bg-amber-100 text-amber-800" : "bg-gray-100 text-gray-500"}`}>
      {label} {on ? "ON" : "off"}
    </span>
  );
}

function CoinRow({ c }: { c: B2Coin }) {
  if (!c.started) {
    return (
      <tr className="border-t border-gray-100">
        <td className="py-2 font-semibold">{c.coin}</td>
        <td colSpan={12} className="py-2 text-gray-400">waiting for the first closed hour…</td>
      </tr>
    );
  }
  const pos = (c.pnl ?? 0) >= 0;
  return (
    <tr className="border-t border-gray-100 font-mono text-sm">
      <td className="py-2 font-sans font-semibold">{c.coin}</td>
      <td className="py-2">{formatCurrency(c.price ?? 0)}</td>
      <td className="py-2">{formatCurrency(c.equity ?? 0)}</td>
      <td className={`py-2 ${pos ? "text-green-600" : "text-red-500"}`}>
        {signed(c.pnl)} ({(c.pnl_pct ?? 0).toFixed(2)}%)
      </td>
      <td className="py-2 font-sans"><Pill on={c.hedge_on} label="hedge" /></td>
      <td className="py-2 font-sans"><Pill on={c.carry_on} label="carry" /></td>
      <td className="py-2">{formatCurrency(c.spot_value ?? 0)}</td>
      <td className="py-2">{signed((c.short_pnl ?? 0) + (c.hedge_realized ?? 0) + (c.funding_on_hedge ?? 0))}</td>
      <td className="py-2">{lev(c.leverage)}</td>
      <td className="py-2">
        {c.hl_account_value != null ? formatCurrency(c.hl_account_value) : "—"}
        <span className="text-xs text-gray-400"> / used {formatCurrency(c.margin_used ?? 0)}</span>
      </td>
      <td className={`py-2 ${c.liq_distance_pct != null && c.liq_distance_pct < 15 ? "text-red-500" : ""}`}
          title={c.liq_price != null ? `liquidation at ${formatCurrency(c.liq_price)}` : "no short open"}>
        {c.liq_distance_pct != null ? `+${c.liq_distance_pct.toFixed(0)}%` : "—"}
      </td>
      <td className="py-2 text-gray-500" title="margin top-ups / liquidations / margin-limited hedges">
        {c.margin_rebalances ?? 0} / <span className={(c.liquidations ?? 0) > 0 ? "text-red-500" : ""}>{c.liquidations ?? 0}</span> / {c.hedge_limited ?? 0}
      </td>
      <td className="py-2 text-gray-500">{formatCurrency(c.fees ?? 0)}</td>
    </tr>
  );
}

export default function B2() {
  const summary = useQuery({ queryKey: ["b2-summary"], queryFn: fetchB2Summary, refetchInterval: 60_000 });
  const equity = useQuery({ queryKey: ["b2-equity"], queryFn: fetchB2Equity, refetchInterval: 60_000 });
  const events = useQuery({ queryKey: ["b2-events"], queryFn: () => fetchB2Events(200), refetchInterval: 60_000 });
  const s = summary.data;

  const points = useMemo(
    () => (equity.data ?? []).map((p) => ({ t: p.ts_ms, label: fmtTs(p.ts_ms), equity: p.equity })),
    [equity.data],
  );
  const pnlPos = (s?.pnl ?? 0) >= 0;

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus="open" route="b2" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          <b>PAPER MODE.</b> Strategy B v2 reads live Hyperliquid prices and funding and simulates fills at the hourly close
          with taker fee + slippage. No order is ever sent — the engine has no signing key.
          {s?.margin_enabled && (
            <div className="mt-1">
              Margin is modelled: each coin is its own HL cross pool holding USDC only (spot is not collateral, so it could sit
              in a cold wallet); shorts open at the leverage below, a pump is topped up by selling spot and cutting the short,
              a spike through the liquidation price liquidates. Same config in backtest: bull 2023-25 +34%/yr, bear 2025-26
              +3%/yr, Jun–Sep 2026 +14%/yr.
            </div>
          )}
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          {summary.isError && <div className="text-red-500">B2 engine unavailable: {String(summary.error)}</div>}
          {s && (
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 text-sm text-gray-500">
              <span>
                Equity{" "}
                <span className="text-xl font-semibold text-gray-900">{s.equity != null ? formatCurrency(s.equity) : "—"}</span>
              </span>
              <span>capital <span className="font-mono">{formatCurrency(s.capital)}</span></span>
              <span className={`font-mono font-semibold ${pnlPos ? "text-green-600" : "text-red-500"}`}>
                P&L {s.pnl != null ? `${signed(s.pnl)} (${(s.pnl_pct ?? 0).toFixed(2)}%)` : "—"}
              </span>
              <span title="P&L since start, annualised linearly — noise until the test has run for weeks">
                APR{" "}
                <span className={`font-mono font-semibold ${(s.apr_pct ?? 0) >= 0 ? "text-green-600" : "text-red-500"}`}>
                  {s.apr_pct != null && s.hours >= 24 ? `${s.apr_pct >= 0 ? "+" : ""}${s.apr_pct.toFixed(1)}%` : "—"}
                </span>
                {s.hours < 24 && <span className="text-xs text-gray-400"> (shown after 24h)</span>}
                {s.hours >= 24 && s.hours < 168 && <span className="text-xs text-amber-600"> (&lt;1 week: noise)</span>}
              </span>
              <span>running <span className="font-mono">{s.hours}h</span> since {fmtTs(s.started_ms)}</span>
              <span>last bar {fmtTs(s.last_bar_ms)}</span>
              <span>status <span className="font-mono">{s.status}</span></span>
              {s.engine_last_error && <span className="text-red-500">engine error: {s.engine_last_error}</span>}
            </div>
          )}
          {s?.margin_enabled && (
            <div className="mt-2 flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm text-gray-500">
              <span>
                short leverage{" "}
                <span className="font-mono text-gray-800">
                  {s.coins.map((c) => `${c.coin} ${lev(c.leverage)}`).join(" · ")}
                </span>
              </span>
              <span>
                spot <span className="font-mono text-gray-800">{s.spot_value != null ? formatCurrency(s.spot_value) : "—"}</span>
                <span className="text-xs text-gray-400"> (cold-wallet-able)</span>
              </span>
              <span>
                on HL <span className="font-mono text-gray-800">{s.hl_account_value != null ? formatCurrency(s.hl_account_value) : "—"}</span>
                <span className="text-xs text-gray-400">
                  {" "}margin used {formatCurrency(s.margin_used ?? 0)} · free {formatCurrency(s.free_margin ?? 0)}
                </span>
              </span>
              <span>
                shorts <span className="font-mono text-gray-800">{formatCurrency(s.short_notional)}</span>
                <span className="text-xs text-gray-400"> · account leverage {lev(s.effective_leverage)}</span>
              </span>
            </div>
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
          <h2 className="mb-2 text-sm font-semibold text-gray-700">Coin books</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-400">
                <tr>
                  <th className="py-1">coin</th><th>price</th><th>equity</th><th>P&L</th><th>hedge</th><th>carry</th>
                  <th>spot</th><th>hedge result</th><th>lev</th><th>HL acct</th><th>to liq</th>
                  <th title="margin top-ups / liquidations / margin-limited hedges">top-up/liq/lim</th><th>fees</th>
                </tr>
              </thead>
              <tbody>{(s?.coins ?? []).map((c) => <CoinRow key={c.coin} c={c} />)}</tbody>
            </table>
          </div>
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          <h2 className="mb-2 text-sm font-semibold text-gray-700">Paper fills</h2>
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-white text-xs uppercase text-gray-400">
                <tr><th className="py-1">time (MSK)</th><th>coin</th><th>action</th><th>notional</th><th>price</th><th>fee</th></tr>
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
