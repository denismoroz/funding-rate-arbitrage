/**
 * AddCoinForm — input ticker + risk params → POST /api/coins.
 *
 * On success: shows the server-discovered facts (spot_token, bridge_safe,
 * sz_decimals) read-only and offers a shortcut link to toggle active.
 * On 422/400/409: shows the server {detail} inline.
 */

import { useState } from "react";
import { useAddCoin, useSetCoinActive } from "../../lib/useCoins";
import type { CoinRow } from "../../lib/api";

interface Props {
  /** Called after a coin is fully added (and optionally activated). */
  onDone?: () => void;
}

export function AddCoinForm({ onDone }: Props) {
  const [coin, setCoin] = useState("");
  const [leverage, setLeverage] = useState("10");
  const [maintRatio, setMaintRatio] = useState("0.05");
  const [positionSizeUsd, setPositionSizeUsd] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<CoinRow | null>(null);
  const [activateError, setActivateError] = useState<string | null>(null);

  const addMutation = useAddCoin();
  const activateMutation = useSetCoinActive();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setServerError(null);
    setDiscovered(null);
    setActivateError(null);

    const lev = parseInt(leverage, 10);
    const maint = parseFloat(maintRatio);
    if (!coin.trim()) { setServerError("Ticker is required."); return; }
    if (!isFinite(lev) || lev < 1 || lev > 50) { setServerError("Leverage must be 1–50."); return; }
    if (!isFinite(maint) || maint <= 0 || maint >= 0.5) { setServerError("Maint ratio must be in (0, 0.5)."); return; }

    const body: Parameters<typeof addMutation.mutate>[0] = {
      coin: coin.trim().toUpperCase(),
      leverage: lev,
      maint_ratio: maint,
    };
    if (positionSizeUsd.trim()) {
      const sz = parseFloat(positionSizeUsd);
      if (!isFinite(sz) || sz <= 0) { setServerError("Position size must be a positive number."); return; }
      body.position_size_usd = sz;
    }

    addMutation.mutate(body, {
      onSuccess: (row) => {
        setDiscovered(row);
        setCoin("");
        setLeverage("10");
        setMaintRatio("0.05");
        setPositionSizeUsd("");
      },
      onError: (err: Error) => {
        setServerError(err.message);
      },
    });
  };

  const handleActivate = () => {
    if (!discovered) return;
    setActivateError(null);
    activateMutation.mutate(
      { coin: discovered.coin, active: true },
      {
        onSuccess: () => {
          setDiscovered(null);
          onDone?.();
        },
        onError: (err: Error) => {
          setActivateError(err.message);
        },
      },
    );
  };

  const handleDismiss = () => {
    setDiscovered(null);
    onDone?.();
  };

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-4 shadow-sm space-y-4">
      <h3 className="text-sm font-semibold text-gray-200">Add Coin</h3>

      {/* Discovered facts panel */}
      {discovered && (
        <div className="rounded border border-emerald-700 bg-emerald-950/40 px-4 py-3 space-y-2 text-xs">
          <div className="font-semibold text-emerald-300">
            {discovered.coin} discovered — currently inactive
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-gray-300">
            <span className="text-gray-400">spot token</span>
            <span className="font-mono">{discovered.spot_token ?? "—"}</span>
            <span className="text-gray-400">sz decimals</span>
            <span className="font-mono">{discovered.sz_decimals ?? "—"}</span>
            <span className="text-gray-400">bridge safe</span>
            <span>{discovered.bridge_safe ? "yes" : "no (perp-only)"}</span>
            <span className="text-gray-400">validated</span>
            <span className={discovered.validated_at ? "text-emerald-400" : "text-gray-500"}>
              {discovered.validated_at ? "yes" : "unvalidated"}
            </span>
          </div>
          {activateError && (
            <p className="text-red-400">{activateError}</p>
          )}
          <div className="flex gap-2 pt-1">
            <button
              type="button"
              onClick={handleActivate}
              disabled={activateMutation.isPending || !discovered.validated_at}
              title={!discovered.validated_at ? "Cannot activate: not yet validated" : undefined}
              className="rounded bg-emerald-700 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {activateMutation.isPending ? "Activating…" : "Activate"}
            </button>
            <button
              type="button"
              onClick={handleDismiss}
              className="rounded border border-gray-600 px-3 py-1 text-xs text-gray-300 hover:bg-gray-700"
            >
              Later
            </button>
          </div>
          <p className="text-gray-500 text-[11px]">
            Activation applies on the next engine cycle.
          </p>
        </div>
      )}

      {!discovered && (
        <form onSubmit={handleSubmit} className="space-y-3">
          {/* Ticker */}
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-gray-400">Ticker</label>
            <input
              type="text"
              value={coin}
              onChange={(e) => setCoin(e.target.value.toUpperCase())}
              placeholder="e.g. BTC"
              className="w-32 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm font-mono uppercase text-gray-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          </div>

          {/* Risk params row */}
          <div className="flex flex-wrap gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-400">Leverage</label>
              <input
                type="number"
                value={leverage}
                min={1}
                max={50}
                step={1}
                onChange={(e) => setLeverage(e.target.value)}
                className="w-24 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-400">Maint ratio</label>
              <input
                type="number"
                value={maintRatio}
                min={0.001}
                max={0.499}
                step={0.001}
                onChange={(e) => setMaintRatio(e.target.value)}
                className="w-28 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-400">
                Position size USD
                <span className="ml-1 text-gray-600">(optional)</span>
              </label>
              <input
                type="number"
                value={positionSizeUsd}
                min={0}
                step={1}
                placeholder="auto"
                onChange={(e) => setPositionSizeUsd(e.target.value)}
                className="w-28 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-400"
              />
            </div>
          </div>

          {serverError && (
            <p className="text-xs text-red-400">{serverError}</p>
          )}

          <div className="flex items-center gap-3 pt-1">
            <button
              type="submit"
              disabled={addMutation.isPending}
              className="rounded bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {addMutation.isPending ? "Discovering…" : "Add & Discover"}
            </button>
            <span className="text-[11px] text-gray-500">
              HL metadata is fetched server-side; spot token and sz decimals are set automatically.
            </span>
          </div>
        </form>
      )}
    </div>
  );
}
