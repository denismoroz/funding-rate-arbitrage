import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategyParams,
  deployStrategyParams,
  forceHourTick,
  type StrategyParams,
  type StrategyParamsHot,
} from "../lib/api";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

// ── Validation ────────────────────────────────────────────────────────────────

type FieldErrors = Partial<Record<keyof StrategyParamsHot, string>>;

function validateHot(values: StrategyParamsHot): FieldErrors {
  const errors: FieldErrors = {};

  if (!(values.entry_threshold > 0 && values.entry_threshold <= 5)) {
    errors.entry_threshold = "Must be > 0 and ≤ 5.";
  }
  if (!(values.exit_threshold >= -2 && values.exit_threshold <= 5 && values.exit_threshold < values.entry_threshold)) {
    errors.exit_threshold = "Must be ≥ −2, ≤ 5, and < entry_threshold.";
  }
  if (
    !Number.isInteger(values.min_hold_hours) ||
    values.min_hold_hours < 0 ||
    values.min_hold_hours > 720
  ) {
    errors.min_hold_hours = "Must be an integer 0–720.";
  }
  if (
    !Number.isInteger(values.concurrency_cap) ||
    values.concurrency_cap < 1 ||
    values.concurrency_cap > 20
  ) {
    errors.concurrency_cap = "Must be an integer 1–20.";
  }
  if (!(values.position_size_usdc > 0 && values.position_size_usdc <= 1_000_000)) {
    errors.position_size_usdc = "Must be > 0 and ≤ 1,000,000.";
  }

  return errors;
}

function hotFromParams(p: StrategyParams): StrategyParamsHot {
  return {
    entry_threshold: p.entry_threshold,
    exit_threshold: p.exit_threshold,
    min_hold_hours: p.min_hold_hours,
    concurrency_cap: p.concurrency_cap,
    position_size_usdc: p.position_size_usdc,
  };
}

function isDirty(a: StrategyParamsHot, b: StrategyParamsHot): boolean {
  return (
    a.entry_threshold !== b.entry_threshold ||
    a.exit_threshold !== b.exit_threshold ||
    a.min_hold_hours !== b.min_hold_hours ||
    a.concurrency_cap !== b.concurrency_cap ||
    a.position_size_usdc !== b.position_size_usdc
  );
}

// ── Form field components ─────────────────────────────────────────────────────

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
  value: number;
  onChange: (v: number) => void;
  step?: number;
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
      onChange={(e) => onChange(Number(e.target.value))}
      className={`rounded border px-3 py-1.5 bg-gray-800 text-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
        hasError ? "border-red-500" : "border-gray-600"
      }`}
    />
  );
}

// ── Settings page ─────────────────────────────────────────────────────────────

export default function Settings() {
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);
  const queryClient = useQueryClient();

  const paramsQ = useQuery({
    queryKey: ["strategy-params", strategyId],
    queryFn: () => fetchStrategyParams(strategyId!),
    enabled: !!strategyId,
  });

  // Form state
  const [form, setForm] = useState<StrategyParamsHot>({
    entry_threshold: 0.3,
    exit_threshold: 0.1,
    min_hold_hours: 4,
    concurrency_cap: 3,
    position_size_usdc: 1000,
  });

  // Sync form from query data on first load
  const [initialized, setInitialized] = useState(false);
  useEffect(() => {
    if (paramsQ.data && !initialized) {
      setForm(hotFromParams(paramsQ.data));
      setInitialized(true);
    }
  }, [paramsQ.data, initialized]);

  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const fieldErrors = validateHot(form);
  const hasFieldErrors = Object.keys(fieldErrors).length > 0;

  const loadedHot = paramsQ.data ? hotFromParams(paramsQ.data) : null;
  const dirty = loadedHot ? isDirty(form, loadedHot) : false;

  function updateField<K extends keyof StrategyParamsHot>(key: K, val: StrategyParamsHot[K]) {
    setForm((prev) => ({ ...prev, [key]: val }));
    setSuccessMsg(null);
    setErrorMsg(null);
  }

  const mutation = useMutation({
    mutationFn: (body: StrategyParamsHot) => deployStrategyParams(strategyId!, body),
    onSuccess: (data) => {
      const now = new Date();
      const hms = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      setSuccessMsg(`Deployed at ${hms}`);
      setErrorMsg(null);
      setForm(hotFromParams(data));
      setInitialized(true);
      void queryClient.invalidateQueries({ queryKey: ["strategy-params", strategyId] });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["events-header"] });
    },
    onError: (err: Error) => {
      setErrorMsg(err.message);
      setSuccessMsg(null);
    },
  });

  function handleDeploy() {
    if (!window.confirm("Apply new params to running strategy?")) return;
    mutation.mutate(form);
  }

  const forceTickMutation = useMutation({
    mutationFn: () => forceHourTick(strategyId!),
    onSuccess: (data) => {
      setSuccessMsg(data.message);
      setErrorMsg(null);
    },
    onError: (err: Error) => {
      setErrorMsg(err.message);
      setSuccessMsg(null);
    },
  });

  const deployDisabled =
    !strategyId ||
    paramsQ.isLoading ||
    !dirty ||
    hasFieldErrors ||
    mutation.isPending;

  return (
    <div className="min-h-screen bg-gray-900">
      <Header wsStatus={status} route="settings" />

      <main className="mx-auto max-w-2xl p-6 space-y-6">
        <h1 className="text-xl font-bold text-white">Strategy Settings</h1>

        {paramsQ.isLoading && (
          <div className="animate-pulse space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-10 rounded bg-gray-700" />
            ))}
          </div>
        )}

        {paramsQ.error instanceof Error && (
          <p className="rounded border border-red-600 bg-red-900/30 p-3 text-sm text-red-400">
            {paramsQ.error.message}
          </p>
        )}

        {paramsQ.data && (
          <>
            {/* Hot params section */}
            <section className="space-y-4">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
                Hot Params
              </h2>

              <FieldRow
                label="Entry Threshold"
                helper="Annualized funding rate. 0.30 = 30% APR."
                error={fieldErrors.entry_threshold}
              >
                <NumberInput
                  value={form.entry_threshold}
                  onChange={(v) => updateField("entry_threshold", v)}
                  step={0.01}
                  hasError={!!fieldErrors.entry_threshold}
                />
              </FieldRow>

              <FieldRow
                label="Exit Threshold"
                helper="Must be less than entry_threshold."
                error={fieldErrors.exit_threshold}
              >
                <NumberInput
                  value={form.exit_threshold}
                  onChange={(v) => updateField("exit_threshold", v)}
                  step={0.01}
                  hasError={!!fieldErrors.exit_threshold}
                />
              </FieldRow>

              <FieldRow
                label="Min Hold Hours"
                helper="Minimum hours before a position is eligible to close."
                error={fieldErrors.min_hold_hours}
              >
                <NumberInput
                  value={form.min_hold_hours}
                  onChange={(v) => updateField("min_hold_hours", Math.round(v))}
                  step={1}
                  min={0}
                  max={720}
                  hasError={!!fieldErrors.min_hold_hours}
                />
              </FieldRow>

              <FieldRow
                label="Concurrency Cap"
                helper="Max concurrent open positions (top-K)."
                error={fieldErrors.concurrency_cap}
              >
                <NumberInput
                  value={form.concurrency_cap}
                  onChange={(v) => updateField("concurrency_cap", Math.round(v))}
                  step={1}
                  min={1}
                  max={20}
                  hasError={!!fieldErrors.concurrency_cap}
                />
              </FieldRow>

              <FieldRow
                label="Position Size (USDC)"
                helper="Notional per leg in USDC."
                error={fieldErrors.position_size_usdc}
              >
                <NumberInput
                  value={form.position_size_usdc}
                  onChange={(v) => updateField("position_size_usdc", v)}
                  step={10}
                  min={0}
                  hasError={!!fieldErrors.position_size_usdc}
                />
              </FieldRow>
            </section>

            {/* Divider */}
            <hr className="border-gray-700" />

            {/* Static params section */}
            <section className="space-y-4">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
                Static Params
                <span className="ml-2 text-xs normal-case font-normal text-gray-500">
                  (requires server restart)
                </span>
              </h2>

              <div className="flex flex-col gap-1">
                <span className="text-sm font-medium text-gray-500">Signal Window Hours</span>
                <span className="rounded border border-gray-700 px-3 py-1.5 bg-gray-800 text-gray-400 text-sm">
                  {paramsQ.data.signal_window_hours}
                </span>
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-sm font-medium text-gray-500">Coins</span>
                <span className="rounded border border-gray-700 px-3 py-1.5 bg-gray-800 text-gray-400 text-sm break-all">
                  {paramsQ.data.coins.join(", ")}
                </span>
              </div>
            </section>

            {/* Deploy button + feedback */}
            <div className="flex flex-col gap-3 pt-2">
              <div className="flex items-center gap-3">
                <button
                  onClick={handleDeploy}
                  disabled={deployDisabled}
                  className={`rounded px-4 py-2 text-sm font-semibold transition-colors ${
                    deployDisabled
                      ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                      : "bg-indigo-600 text-white hover:bg-indigo-500"
                  }`}
                >
                  {mutation.isPending ? "Deploying…" : "Deploy"}
                </button>
                <button
                  onClick={() => forceTickMutation.mutate()}
                  disabled={forceTickMutation.isPending || !strategyId}
                  title="Schedule an hour-tick on the next minute boundary (≤60s) — runs funding fetch + open/close decisions without waiting for the real hourly boundary."
                  className={`rounded px-4 py-2 text-sm font-semibold transition-colors ${
                    forceTickMutation.isPending
                      ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                      : "border border-gray-600 bg-gray-800 text-gray-200 hover:bg-gray-700"
                  }`}
                >
                  {forceTickMutation.isPending ? "Scheduling…" : "Force Hour Tick"}
                </button>
              </div>

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
