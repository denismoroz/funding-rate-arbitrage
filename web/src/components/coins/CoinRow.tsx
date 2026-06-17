/**
 * CoinRow — a single row in the Coin Registry table.
 *
 * Risk fields (leverage, maint_ratio, position_size_usd) are editable inline
 * with save/cancel. Market facts (spot_token, sz_decimals, bridge_safe,
 * validated_at) are read-only. Active is a toggle. Remove is guarded.
 */

import { useState } from "react";
import { usePatchCoin, useSetCoinActive, useDeleteCoin } from "../../lib/useCoins";
import type { CoinRow as CoinRowType } from "../../lib/api";

interface Props {
  row: CoinRowType;
}

/** Green badge if validated; muted "unvalidated" otherwise. */
function ValidatedBadge({ validatedAt }: { validatedAt: number | null }) {
  if (validatedAt) {
    return (
      <span className="inline-block rounded-full bg-emerald-900 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
        validated
      </span>
    );
  }
  return (
    <span className="inline-block rounded-full bg-gray-700 px-2 py-0.5 text-[11px] text-gray-500">
      unvalidated
    </span>
  );
}

/** Small bridge-safe badge. */
function BridgeBadge({ bridgeSafe, spotToken }: { bridgeSafe: boolean; spotToken: string | null }) {
  if (bridgeSafe) {
    return (
      <span className="inline-block rounded bg-sky-900 px-1.5 py-0.5 text-[11px] text-sky-300">
        bridge-safe
      </span>
    );
  }
  if (!spotToken) {
    return (
      <span className="inline-block rounded bg-gray-700 px-1.5 py-0.5 text-[11px] text-gray-400">
        perp-only
      </span>
    );
  }
  return (
    <span className="inline-block rounded bg-amber-900 px-1.5 py-0.5 text-[11px] text-amber-300">
      not bridge-safe
    </span>
  );
}

export function CoinRow({ row }: Props) {
  const patchMutation = usePatchCoin();
  const activateMutation = useSetCoinActive();
  const deleteMutation = useDeleteCoin();

  // Inline edit state
  const [editing, setEditing] = useState(false);
  const [leverage, setLeverage] = useState(String(row.leverage));
  const [maintRatio, setMaintRatio] = useState(String(row.maint_ratio));
  const [positionSizeUsd, setPositionSizeUsd] = useState(
    row.position_size_usd != null ? String(row.position_size_usd) : "",
  );
  const [editError, setEditError] = useState<string | null>(null);

  // Active toggle state
  const [activeError, setActiveError] = useState<string | null>(null);

  // Remove state
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const handleEditStart = () => {
    setLeverage(String(row.leverage));
    setMaintRatio(String(row.maint_ratio));
    setPositionSizeUsd(row.position_size_usd != null ? String(row.position_size_usd) : "");
    setEditError(null);
    setEditing(true);
  };

  const handleEditCancel = () => {
    setEditing(false);
    setEditError(null);
  };

  const handleEditSave = () => {
    setEditError(null);
    const lev = parseInt(leverage, 10);
    const maint = parseFloat(maintRatio);
    if (!isFinite(lev) || lev < 1 || lev > 50) {
      setEditError("Leverage must be 1–50.");
      return;
    }
    if (!isFinite(maint) || maint <= 0 || maint >= 0.5) {
      setEditError("Maint ratio must be in (0, 0.5).");
      return;
    }
    const body: Parameters<typeof patchMutation.mutate>[0]["body"] = {
      leverage: lev,
      maint_ratio: maint,
    };
    if (positionSizeUsd.trim()) {
      const sz = parseFloat(positionSizeUsd);
      if (!isFinite(sz) || sz <= 0) {
        setEditError("Position size must be a positive number.");
        return;
      }
      body.position_size_usd = sz;
    } else {
      body.position_size_usd = null;
    }
    patchMutation.mutate(
      { coin: row.coin, body },
      {
        onSuccess: () => setEditing(false),
        onError: (err: Error) => setEditError(err.message),
      },
    );
  };

  const handleToggleActive = () => {
    setActiveError(null);
    activateMutation.mutate(
      { coin: row.coin, active: !row.active },
      {
        onError: (err: Error) => setActiveError(err.message),
      },
    );
  };

  const handleRemove = () => {
    if (!confirmRemove) {
      setConfirmRemove(true);
      return;
    }
    setRemoveError(null);
    deleteMutation.mutate(row.coin, {
      onError: (err: Error) => {
        setRemoveError(err.message);
        setConfirmRemove(false);
      },
    });
  };

  return (
    <>
      <tr className="border-t border-gray-700 hover:bg-gray-750 transition-colors">
        {/* Coin */}
        <td className="px-3 py-2 font-mono text-sm font-semibold text-gray-100">
          {row.coin}
        </td>

        {/* Leverage */}
        <td className="px-3 py-2 text-sm text-gray-200">
          {editing ? (
            <input
              type="number"
              value={leverage}
              min={1}
              max={50}
              step={1}
              onChange={(e) => setLeverage(e.target.value)}
              className="w-16 rounded border border-gray-600 bg-gray-900 px-1.5 py-0.5 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          ) : (
            <span>{row.leverage}×</span>
          )}
        </td>

        {/* Maint ratio */}
        <td className="px-3 py-2 text-sm text-gray-200">
          {editing ? (
            <input
              type="number"
              value={maintRatio}
              min={0.001}
              max={0.499}
              step={0.001}
              onChange={(e) => setMaintRatio(e.target.value)}
              className="w-24 rounded border border-gray-600 bg-gray-900 px-1.5 py-0.5 text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          ) : (
            <span>{row.maint_ratio}</span>
          )}
        </td>

        {/* Position size USD */}
        <td className="px-3 py-2 text-sm text-gray-200">
          {editing ? (
            <input
              type="number"
              value={positionSizeUsd}
              min={0}
              step={1}
              placeholder="auto"
              onChange={(e) => setPositionSizeUsd(e.target.value)}
              className="w-24 rounded border border-gray-600 bg-gray-900 px-1.5 py-0.5 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          ) : (
            <span className={row.position_size_usd == null ? "text-gray-500 italic" : ""}>
              {row.position_size_usd != null ? `$${row.position_size_usd}` : "auto"}
            </span>
          )}
        </td>

        {/* Active toggle */}
        <td className="px-3 py-2">
          <button
            type="button"
            role="switch"
            aria-checked={row.active}
            disabled={activateMutation.isPending}
            onClick={handleToggleActive}
            title={row.active ? "Deactivate" : "Activate"}
            className={[
              "relative inline-flex h-[20px] w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent",
              "transition-colors duration-200 focus:outline-none",
              row.active ? "bg-emerald-500" : "bg-slate-600",
              activateMutation.isPending ? "opacity-50 cursor-not-allowed" : "",
            ].join(" ")}
          >
            <span
              className={[
                "pointer-events-none inline-block h-[16px] w-[16px] rounded-full bg-white shadow-sm",
                "transform transition-transform duration-200",
                row.active ? "translate-x-[16px]" : "translate-x-0",
              ].join(" ")}
            />
          </button>
        </td>

        {/* Spot token (read-only) */}
        <td className="px-3 py-2 text-xs font-mono text-gray-400">
          {row.spot_token ?? "—"}
        </td>

        {/* sz_decimals (read-only) */}
        <td className="px-3 py-2 text-xs text-gray-400 text-right">
          {row.sz_decimals ?? "—"}
        </td>

        {/* Bridge safe badge */}
        <td className="px-3 py-2">
          <BridgeBadge bridgeSafe={row.bridge_safe} spotToken={row.spot_token} />
        </td>

        {/* Validated badge */}
        <td className="px-3 py-2">
          <ValidatedBadge validatedAt={row.validated_at} />
        </td>

        {/* Actions */}
        <td className="px-3 py-2">
          <div className="flex items-center gap-1.5">
            {editing ? (
              <>
                <button
                  type="button"
                  onClick={handleEditSave}
                  disabled={patchMutation.isPending}
                  className="rounded bg-indigo-600 px-2 py-0.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  {patchMutation.isPending ? "…" : "Save"}
                </button>
                <button
                  type="button"
                  onClick={handleEditCancel}
                  className="rounded border border-gray-600 px-2 py-0.5 text-xs text-gray-300 hover:bg-gray-700"
                >
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={handleEditStart}
                  className="rounded border border-gray-600 px-2 py-0.5 text-xs text-gray-300 hover:bg-gray-700"
                >
                  Edit
                </button>
                {confirmRemove ? (
                  <button
                    type="button"
                    onClick={handleRemove}
                    disabled={deleteMutation.isPending}
                    className="rounded bg-red-700 px-2 py-0.5 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
                  >
                    {deleteMutation.isPending ? "…" : "Confirm"}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => { setConfirmRemove(true); setRemoveError(null); }}
                    className="rounded border border-red-800 px-2 py-0.5 text-xs text-red-400 hover:bg-red-900/40"
                  >
                    Remove
                  </button>
                )}
                {confirmRemove && !deleteMutation.isPending && (
                  <button
                    type="button"
                    onClick={() => setConfirmRemove(false)}
                    className="text-xs text-gray-500 hover:text-gray-300"
                  >
                    ✕
                  </button>
                )}
              </>
            )}
          </div>
        </td>
      </tr>

      {/* Inline error row (spans all columns) */}
      {(editError || activeError || removeError) && (
        <tr className="border-t border-red-900/40">
          <td colSpan={10} className="px-3 pb-2 pt-1">
            <p className="rounded border border-red-700 bg-red-950/40 px-3 py-1 text-xs text-red-400">
              {editError ?? activeError ?? removeError}
            </p>
          </td>
        </tr>
      )}
    </>
  );
}
