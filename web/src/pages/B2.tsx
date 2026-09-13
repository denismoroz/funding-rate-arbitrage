import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { Header } from "../components/Header";
import { fetchB2Events, fetchB2Equity, fetchB2Summary, type B2Coin, type B2Summary, type B2Test } from "../lib/api";
import { formatCurrency } from "../lib/format";

const TEST_META: Record<B2Test, { title: string; backtest: string }> = {
  b2: {
    title: "Main test — spot + trend hedge + funding carry on the idle cash",
    backtest: "rising market +34%/yr, falling market +3%/yr, Jun–Sep 2026 +14%/yr",
  },
  b2_cold: {
    title: "Cold wallet test — spot (could sit off-exchange) + trend hedge on HL, no carry",
    backtest: "rising market +39%/yr, falling market +3%/yr, Jun–Sep 2026 +18%/yr",
  },
};

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

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-gray-100 bg-gray-50/60 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">{title}</div>
      {children}
    </div>
  );
}

function ResultPanel({ s }: { s: B2Summary }) {
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
            {(s.apr_pct ?? 0) >= 0 ? "+" : ""}{(s.apr_pct ?? 0).toFixed(1)}%
          </span>
        ) : (
          <span className="text-gray-400">appears after 24h</span>
        )}
        {aprReady && s.hours < 168 && <span className="text-xs text-amber-600"> · first week is noise</span>}
      </div>
      <div className="mt-2 text-xs text-gray-400">
        running {s.hours}h since {fmtTs(s.started_ms)} MSK
        {s.status !== "active" && <span className="text-amber-600"> · {s.status}</span>}
      </div>
      {s.engine_last_error && <div className="mt-1 text-xs text-red-500">engine error: {s.engine_last_error}</div>}
    </Panel>
  );
}

function MoneyPanel({ s }: { s: B2Summary }) {
  const carry = s.params.carry_enabled === true;
  const parts = [
    { label: "Spot coins", hint: "could sit in a cold wallet", value: s.spot_value ?? 0, color: "bg-blue-500" },
    { label: "HL: margin for open shorts", hint: "USDC locked by hedges / carry", value: s.margin_used ?? 0, color: "bg-amber-500" },
    { label: "HL: free USDC", hint: carry ? "for the next hedge, carry, top-ups" : "for the next hedge and top-ups", value: Math.max(s.free_margin ?? 0, 0), color: "bg-gray-300" },
  ];
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  return (
    <Panel title="Where the money is">
      <div className="flex h-3 overflow-hidden rounded-full bg-gray-100">
        {parts.map((p) => (
          <div key={p.label} className={p.color} style={{ width: `${(p.value / total) * 100}%` }} />
        ))}
      </div>
      <ul className="mt-3 space-y-1.5 text-sm">
        {parts.map((p) => (
          <li key={p.label} className="flex items-baseline gap-2">
            <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-sm ${p.color}`} />
            <span className="text-gray-700">{p.label}</span>
            <span className="text-xs text-gray-400">{p.hint}</span>
            <span className="ml-auto font-mono text-gray-900">{formatCurrency(p.value)}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function ProtectionPanel({ s }: { s: B2Summary }) {
  const started = s.coins.filter((c) => c.started);
  const hedged = started.filter((c) => c.hedge_on).map((c) => c.coin);
  const open = started.filter((c) => !c.hedge_on).map((c) => c.coin);
  const carry = started.filter((c) => c.carry_on).map((c) => c.coin);
  const nearest = started
    .filter((c) => c.liq_distance_pct != null)
    .sort((a, b) => (a.liq_distance_pct ?? 0) - (b.liq_distance_pct ?? 0))[0];
  return (
    <Panel title="Protection now">
      <dl className="space-y-1.5 text-sm">
        <div className="flex gap-2">
          <dt className="w-32 shrink-0 text-gray-500">Hedged</dt>
          <dd className="font-medium text-gray-900">{hedged.length ? hedged.join(", ") : "none"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-32 shrink-0 text-gray-500">Not hedged</dt>
          <dd className="text-gray-700">{open.length ? open.join(", ") : "none"}</dd>
        </div>
        {s.params.carry_enabled === true && (
          <div className="flex gap-2">
            <dt className="w-32 shrink-0 text-gray-500">Carry on</dt>
            <dd className="text-gray-700">{carry.length ? carry.join(", ") : "none"}</dd>
          </div>
        )}
        <div className="flex gap-2">
          <dt className="w-32 shrink-0 text-gray-500">Short leverage</dt>
          <dd className="font-mono text-gray-700">{started.map((c) => `${c.coin} ${lev(c.leverage)}`).join("  ")}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-32 shrink-0 text-gray-500">Liquidation</dt>
          <dd className={nearest && (nearest.liq_distance_pct ?? 0) < 15 ? "text-red-500" : "text-gray-700"}>
            {nearest
              ? <>nearest {nearest.coin}: price must rise <b>+{(nearest.liq_distance_pct ?? 0).toFixed(0)}%</b></>
              : "no shorts open"}
          </dd>
        </div>
      </dl>
    </Panel>
  );
}

export default function B2({ test }: { test: B2Test }) {
  const summary = useQuery({ queryKey: ["b2-summary", test], queryFn: () => fetchB2Summary(test), refetchInterval: 60_000 });
  const equity = useQuery({ queryKey: ["b2-equity", test], queryFn: () => fetchB2Equity(test), refetchInterval: 60_000 });
  const events = useQuery({ queryKey: ["b2-events", test], queryFn: () => fetchB2Events(200, test), refetchInterval: 60_000 });
  const meta = TEST_META[test];
  const s = summary.data;

  const points = useMemo(
    () => (equity.data ?? []).map((p) => ({ t: p.ts_ms, label: fmtTs(p.ts_ms), equity: p.equity })),
    [equity.data],
  );

  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus="open" route={test === "b2_cold" ? "b2-cold" : "b2"} />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          <div className="font-semibold">{meta.title}</div>
          <b>PAPER MODE</b> — live Hyperliquid prices and funding, simulated fills, no real orders. Same config in backtest:{" "}
          {meta.backtest}.
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
          {summary.isError && <div className="text-red-500">B2 engine unavailable: {String(summary.error)}</div>}
          {s && (
            <div className="grid gap-4 md:grid-cols-3">
              <ResultPanel s={s} />
              {s.margin_enabled && <MoneyPanel s={s} />}
              {s.margin_enabled && <ProtectionPanel s={s} />}
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
