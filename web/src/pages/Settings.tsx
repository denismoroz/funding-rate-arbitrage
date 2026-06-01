import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategy,
  patchStrategyParams,
  type ParamValue,
} from "../lib/api";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

// ── Field layout helper ───────────────────────────────────────────────────────

function FieldRow({
  label,
  helper,
  error,
  children,
}: {
  label: string;
  helper?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-200">{label}</label>
      {children}
      {helper && !error && (
        <p className="text-xs text-gray-500">{helper}</p>
      )}
      {error && (
        <p className="text-xs text-red-400">{error}</p>
      )}
    </div>
  );
}

function NumberInput({
  value,
  onChange,
  step,
  min,
  max,
  hasError,
}: {
  value: string;
  onChange: (v: string) => void;
  step?: string;
  min?: number;
  max?: number;
  hasError?: boolean;
}) {
  return (
    <input
      type="number"
      value={value}
      step={step}
      min={min}
      max={max}
      onChange={(e) => onChange(e.target.value)}
      className={`rounded border px-3 py-1.5 bg-gray-800 text-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
        hasError ? "border-red-500" : "border-gray-600"
      }`}
    />
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  hasError,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  hasError?: boolean;
}) {
  return (
    <input
      type="text"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={`rounded border px-3 py-1.5 bg-gray-800 text-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
        hasError ? "border-red-500" : "border-gray-600"
      }`}
    />
  );
}

// ── Field definitions for TwoPhaseParams ─────────────────────────────────────

type FieldType = "float" | "int" | "coins";

interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  helper?: string;
  min?: number;
  max?: number;
  step?: string;
  group: "capital" | "entry_exit" | "phase1" | "phase2";
}

const FIELD_DEFS: FieldDef[] = [
  // Capital
  {
    key: "budget_cap_usdc",
    label: "Budget cap (USDC)",
    type: "float",
    min: 0,
    step: "1000",
    helper: "Max total committed capital across open + pending positions",
    group: "capital",
  },
  {
    key: "margin_buffer_factor",
    label: "Margin buffer factor",
    type: "float",
    min: 1,
    step: "0.5",
    helper: "Perp margin = size / leverage × buffer",
    group: "capital",
  },
  {
    key: "concurrency_cap",
    label: "Concurrency cap (K)",
    type: "int",
    min: 1,
    step: "1",
    helper: "Max simultaneous open FarbPositions",
    group: "capital",
  },
  // Entry / Exit
  {
    key: "coins",
    label: "Coins (comma-separated)",
    type: "coins",
    helper: "e.g. BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE",
    group: "entry_exit",
  },
  {
    key: "entry_threshold_apr",
    label: "Entry threshold APR",
    type: "float",
    min: 0,
    max: 1,
    step: "0.01",
    helper: "Enter when smoothed signal APR > this (e.g. 0.10 = 10%)",
    group: "entry_exit",
  },
  {
    key: "signal_window_hours",
    label: "Signal window (hours)",
    type: "int",
    min: 1,
    step: "1",
    helper: "Rolling window for smoothed funding signal",
    group: "entry_exit",
  },
  // Phase 1
  {
    key: "base_min_hold_hours",
    label: "Base min hold (hours)",
    type: "int",
    min: 1,
    step: "1",
    helper: "Floor on dynamic min-hold duration",
    group: "phase1",
  },
  {
    key: "cap_min_hold_hours",
    label: "Cap min hold (hours)",
    type: "int",
    min: 1,
    step: "24",
    helper: "Ceiling on dynamic min-hold duration",
    group: "phase1",
  },
  {
    key: "safety_mult",
    label: "Safety multiplier",
    type: "float",
    min: 1,
    step: "0.5",
    helper: "Breakeven-based min-hold multiplier",
    group: "phase1",
  },
  {
    key: "phase1_negative_patience",
    label: "Phase1 negative patience (hours)",
    type: "int",
    min: 1,
    step: "1",
    helper: "Hours of consecutive negative signal before phase1 exit",
    group: "phase1",
  },
  {
    key: "phase1_breakeven_cap_hours",
    label: "Phase1 breakeven cap (hours)",
    type: "int",
    min: 1,
    step: "24",
    helper: "If hours-to-breakeven > this → exit phase1",
    group: "phase1",
  },
  // Phase 2
  {
    key: "phase2_exit_threshold",
    label: "Phase2 exit threshold APR",
    type: "float",
    min: -1,
    max: 0,
    step: "0.01",
    helper: "Exit (phase2) when smoothed signal APR < this (e.g. -0.10 = -10%)",
    group: "phase2",
  },
];

const GROUP_LABELS: Record<string, string> = {
  capital: "Capital & Sizing",
  entry_exit: "Entry / Exit Signal",
  phase1: "Phase 1 — Hold Logic",
  phase2: "Phase 2 — Exit",
};

// ── Validation ────────────────────────────────────────────────────────────────

function validateField(
  def: FieldDef,
  raw: string,
): { val: number | string[]; error: null } | { val: null; error: string } {
  if (def.type === "coins") {
    const parts = raw.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (parts.length === 0) {
      return { val: null, error: "At least one coin required" };
    }
    return { val: parts, error: null };
  }

  const num = def.type === "int" ? parseInt(raw, 10) : parseFloat(raw);
  if (!isFinite(num)) {
    return { val: null, error: `Invalid ${def.type}` };
  }
  if (def.min != null && num < def.min) {
    return { val: null, error: `Must be >= ${def.min}` };
  }
  if (def.max != null && num > def.max) {
    return { val: null, error: `Must be <= ${def.max}` };
  }
  if (def.type === "int" && !Number.isInteger(num)) {
    return { val: null, error: "Must be an integer" };
  }
  return { val: num, error: null };
}

// ── Settings page ─────────────────────────────────────────────────────────────

export default function Settings() {
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);
  const queryClient = useQueryClient();

  const stratQ = useQuery({
    queryKey: ["strategy", strategyId],
    queryFn: () => fetchStrategy(strategyId!),
    enabled: !!strategyId,
  });

  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});

  // Sync server → form when data loads
  useEffect(() => {
    if (!stratQ.data) return;
    const params = stratQ.data.params_json;
    const init: Record<string, string> = {};
    for (const def of FIELD_DEFS) {
      const v = params[def.key];
      if (def.type === "coins") {
        init[def.key] = Array.isArray(v) ? (v as string[]).join(", ") : String(v ?? "");
      } else {
        init[def.key] = v != null ? String(v) : "";
      }
    }
    setFormValues(init);
    setFormErrors({});
  }, [stratQ.data]);

  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const patchMutation = useMutation({
    mutationFn: (body: Record<string, ParamValue>) =>
      patchStrategyParams(strategyId!, { params: body }),
    onSuccess: (data) => {
      const now = new Date();
      const hms = now.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      setSuccessMsg(
        `Params updated at ${hms} — restart engine for changes to take effect`,
      );
      setErrorMsg(null);
      // Sync form to returned params
      const returned = data.params_json;
      const updated: Record<string, string> = {};
      for (const def of FIELD_DEFS) {
        const v = returned[def.key];
        if (def.type === "coins") {
          updated[def.key] = Array.isArray(v) ? (v as string[]).join(", ") : String(v ?? "");
        } else {
          updated[def.key] = v != null ? String(v) : "";
        }
      }
      setFormValues(updated);
      setFormErrors({});
      void queryClient.invalidateQueries({ queryKey: ["strategy", strategyId] });
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (e: Error) => {
      setErrorMsg(e.message);
      setSuccessMsg(null);
    },
  });

  function handleFieldChange(key: string, raw: string) {
    setFormValues((prev) => ({ ...prev, [key]: raw }));
    setSuccessMsg(null);
    setErrorMsg(null);
  }

  function handleSubmit() {
    const body: Record<string, ParamValue> = {};
    const errors: Record<string, string> = {};

    for (const def of FIELD_DEFS) {
      const raw = formValues[def.key] ?? "";
      const result = validateField(def, raw);
      if (result.error !== null) {
        errors[def.key] = result.error;
      } else {
        body[def.key] = result.val;
      }
    }

    setFormErrors(errors);
    if (Object.keys(errors).length === 0) {
      patchMutation.mutate(body);
    }
  }

  // Dirty check
  const isDirty = (() => {
    if (!stratQ.data) return false;
    const params = stratQ.data.params_json;
    for (const def of FIELD_DEFS) {
      const server = params[def.key];
      const serverStr = def.type === "coins"
        ? Array.isArray(server) ? (server as string[]).join(", ") : String(server ?? "")
        : server != null ? String(server) : "";
      if (formValues[def.key] !== serverStr) return true;
    }
    return false;
  })();

  const hasFieldErrors = Object.keys(formErrors).length > 0;
  const submitDisabled =
    !strategyId ||
    stratQ.isLoading ||
    !isDirty ||
    hasFieldErrors ||
    patchMutation.isPending;

  // Per-position footprint preview (auto-derived — mirrors TwoPhaseParams.compute_size_for).
  // Leverage table mirrors src/frab/constants.py RESEARCH_LEVERAGE.
  const RESEARCH_LEVERAGE: Record<string, number> = {
    BTC: 40, ETH: 25, SOL: 20, HYPE: 10, ZEC: 10, PURR: 3, XPL: 10,
  };
  const footprintPreview = (() => {
    const budget = parseFloat(formValues["budget_cap_usdc"] ?? "");
    const K = parseFloat(formValues["concurrency_cap"] ?? "");
    const buf = parseFloat(formValues["margin_buffer_factor"] ?? "");
    const coinsStr = formValues["coins"] ?? "";
    if (!isFinite(budget) || !isFinite(K) || !isFinite(buf) || K === 0) return null;
    const slot = budget / K;
    const coins = coinsStr.split(",").map((c) => c.trim().toUpperCase()).filter(Boolean);
    const perCoin = coins.map((coin) => {
      const lev = RESEARCH_LEVERAGE[coin] ?? 3;
      const size = slot / (1 + buf / lev);
      const margin = slot - size;
      return { coin, lev, size, margin };
    });
    return { slot, buf, perCoin };
  })();

  // Group fields
  const groups = ["capital", "entry_exit", "phase1", "phase2"] as const;

  return (
    <div className="min-h-screen bg-gray-900">
      <Header wsStatus={status} route="settings" />

      <main className="mx-auto max-w-2xl p-6 space-y-6">
        <h1 className="text-xl font-bold text-white">Strategy Settings</h1>

        {stratQ.isLoading && (
          <div className="animate-pulse space-y-3">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-10 rounded bg-gray-700" />
            ))}
          </div>
        )}

        {stratQ.error instanceof Error && (
          <p className="rounded border border-red-600 bg-red-900/30 p-3 text-sm text-red-400">
            {stratQ.error.message}
          </p>
        )}

        {stratQ.data && (
          <>
            {/* Strategy info banner */}
            <div className="rounded border border-gray-700 bg-gray-800 px-4 py-2 text-xs text-gray-400 flex items-center gap-4">
              <span className="font-semibold text-gray-200">{stratQ.data.name}</span>
              <span>v{stratQ.data.version}</span>
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                  stratQ.data.status === "running"
                    ? "bg-green-800 text-green-200"
                    : "bg-gray-700 text-gray-400"
                }`}
              >
                {stratQ.data.status}
              </span>
            </div>

            {/* Per-position footprint preview */}
            {footprintPreview != null && (
              <div className="rounded border border-indigo-700 bg-indigo-900/30 px-4 py-2 text-xs text-indigo-300 space-y-1">
                <div>
                  Per-position slot:{" "}
                  <span className="font-semibold text-indigo-200">
                    ${footprintPreview.slot.toFixed(2)} USDC
                  </span>
                  {" "}(budget_cap ÷ K). Size auto-derived per coin from{" "}
                  <code className="text-indigo-200">slot / (1 + {footprintPreview.buf}/leverage)</code>:
                </div>
                {footprintPreview.perCoin.length > 0 && (
                  <table className="text-[11px] font-mono text-indigo-200">
                    <thead className="text-indigo-400">
                      <tr>
                        <th className="px-2 text-left">coin</th>
                        <th className="px-2 text-right">lev</th>
                        <th className="px-2 text-right">size</th>
                        <th className="px-2 text-right">margin</th>
                      </tr>
                    </thead>
                    <tbody>
                      {footprintPreview.perCoin.map((row) => (
                        <tr key={row.coin}>
                          <td className="px-2">{row.coin}</td>
                          <td className="px-2 text-right">{row.lev}×</td>
                          <td className="px-2 text-right">${row.size.toFixed(2)}</td>
                          <td className="px-2 text-right">${row.margin.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* Grouped param sections */}
            {groups.map((group) => {
              const fields = FIELD_DEFS.filter((f) => f.group === group);
              return (
                <section key={group} className="space-y-4">
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400 border-b border-gray-700 pb-1">
                    {GROUP_LABELS[group]}
                  </h2>
                  {fields.map((def) => (
                    <FieldRow
                      key={def.key}
                      label={def.label}
                      helper={def.helper}
                      error={formErrors[def.key]}
                    >
                      {def.type === "coins" ? (
                        <TextInput
                          value={formValues[def.key] ?? ""}
                          onChange={(v) => handleFieldChange(def.key, v)}
                          placeholder="BTC, ETH, SOL, ..."
                          hasError={!!formErrors[def.key]}
                        />
                      ) : (
                        <NumberInput
                          value={formValues[def.key] ?? ""}
                          onChange={(v) => handleFieldChange(def.key, v)}
                          step={def.step}
                          min={def.min}
                          max={def.max}
                          hasError={!!formErrors[def.key]}
                        />
                      )}
                    </FieldRow>
                  ))}
                </section>
              );
            })}

            {/* Submit */}
            <div className="flex flex-col gap-3 pt-2">
              <button
                onClick={handleSubmit}
                disabled={submitDisabled}
                className={`rounded px-4 py-2 text-sm font-semibold transition-colors w-fit ${
                  submitDisabled
                    ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                    : "bg-indigo-600 text-white hover:bg-indigo-500"
                }`}
              >
                {patchMutation.isPending ? "Saving…" : "Save Params"}
              </button>

              {successMsg && (
                <p className="text-sm text-green-400">{successMsg}</p>
              )}
              {errorMsg && (
                <p className="rounded border border-red-600 bg-red-900/30 p-2 text-sm text-red-400">
                  {errorMsg}
                </p>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
