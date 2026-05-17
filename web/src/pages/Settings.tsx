import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategyParams,
  deployStrategyParams,
  forceHourTick,
  type HotFieldSpec,
} from "../lib/api";
import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "./Dashboard";

// ── Field components ──────────────────────────────────────────────────────────

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

// ── Validation helper ─────────────────────────────────────────────────────────

function validateField(
  _key: string,
  raw: string,
  spec: HotFieldSpec,
): { val: number; error: null } | { val: null; error: string } {
  const num = spec.type === "int" ? parseInt(raw, 10) : parseFloat(raw);
  if (!isFinite(num)) {
    return { val: null, error: `Invalid ${spec.type}` };
  }
  if (spec.min_value !== null) {
    if (spec.exclusive_min ? num <= spec.min_value : num < spec.min_value) {
      return {
        val: null,
        error: `Must be ${spec.exclusive_min ? ">" : ">="} ${spec.min_value}`,
      };
    }
  }
  if (spec.max_value !== null) {
    if (spec.exclusive_max ? num >= spec.max_value : num > spec.max_value) {
      return {
        val: null,
        error: `Must be ${spec.exclusive_max ? "<" : "<="} ${spec.max_value}`,
      };
    }
  }
  // For int, require actual integer value
  if (spec.type === "int" && !Number.isInteger(num)) {
    return { val: null, error: "Must be an integer" };
  }
  return { val: num, error: null };
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

  // Local form state: string values to allow partial editing
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});

  // Sync server → form when data loads or strategy changes
  useEffect(() => {
    if (!paramsQ.data) return;
    const init: Record<string, string> = {};
    for (const key of Object.keys(paramsQ.data.hot_schema)) {
      init[key] = String(paramsQ.data.params[key] ?? "");
    }
    setFormValues(init);
    setFormErrors({});
  }, [paramsQ.data]);

  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const deployMutation = useMutation({
    mutationFn: (body: Record<string, number>) =>
      deployStrategyParams(strategyId!, body),
    onSuccess: (data) => {
      const now = new Date();
      const hms = now.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      setSuccessMsg(`Deployed at ${hms}`);
      setErrorMsg(null);
      // Sync form to returned params
      if (paramsQ.data) {
        const updated: Record<string, string> = {};
        for (const key of Object.keys(paramsQ.data.hot_schema)) {
          updated[key] = String(data.params[key] ?? "");
        }
        setFormValues(updated);
        setFormErrors({});
      }
      void queryClient.invalidateQueries({ queryKey: ["strategy-params", strategyId] });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["events-header"] });
    },
    onError: (e: Error) => {
      setErrorMsg(e.message);
      setSuccessMsg(null);
    },
  });

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

  function handleFieldChange(key: string, raw: string) {
    setFormValues((prev) => ({ ...prev, [key]: raw }));
    setSuccessMsg(null);
    setErrorMsg(null);
  }

  function handleSubmit() {
    if (!paramsQ.data) return;
    const schema = paramsQ.data.hot_schema;
    const body: Record<string, number> = {};
    const errors: Record<string, string> = {};

    for (const [key, spec] of Object.entries(schema)) {
      const raw = formValues[key] ?? "";
      const result = validateField(key, raw, spec);
      if (result.error !== null) {
        errors[key] = result.error;
      } else {
        body[key] = result.val;
      }
    }

    setFormErrors(errors);
    if (Object.keys(errors).length === 0) {
      if (!window.confirm("Apply new params to running strategy?")) return;
      deployMutation.mutate(body);
    }
  }

  // Dirty check: any hot field differs from server value
  const isDirty = (() => {
    if (!paramsQ.data) return false;
    for (const key of Object.keys(paramsQ.data.hot_schema)) {
      if (formValues[key] !== String(paramsQ.data.params[key] ?? "")) {
        return true;
      }
    }
    return false;
  })();

  const hasFieldErrors = Object.keys(formErrors).length > 0;

  const deployDisabled =
    !strategyId ||
    paramsQ.isLoading ||
    !isDirty ||
    hasFieldErrors ||
    deployMutation.isPending;

  const hotSchema = paramsQ.data?.hot_schema ?? {};
  const hotSchemaKeys = Object.keys(hotSchema);
  const coldParams = paramsQ.data
    ? Object.entries(paramsQ.data.params).filter(([k]) => !hotSchemaKeys.includes(k))
    : [];

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
            {hotSchemaKeys.length === 0 ? (
              /* Unknown/legacy strategy — read-only view */
              <section className="space-y-3">
                <p className="text-sm text-amber-400">
                  ⚠️ No hot-deployable params for this strategy.
                </p>
                <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
                  Params (read-only)
                </h2>
                <pre className="rounded border border-gray-700 bg-gray-800 p-3 text-xs text-gray-300 overflow-auto">
                  {JSON.stringify(paramsQ.data.params, null, 2)}
                </pre>
              </section>
            ) : (
              <>
                {/* Hot params section */}
                <section className="space-y-4">
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
                    Hot Params
                  </h2>

                  {hotSchemaKeys.map((key) => {
                    const spec = hotSchema[key];
                    return (
                      <FieldRow
                        key={key}
                        label={spec.label}
                        helper={spec.description || undefined}
                        error={formErrors[key]}
                      >
                        <NumberInput
                          value={formValues[key] ?? ""}
                          onChange={(v) => handleFieldChange(key, v)}
                          step={spec.type === "int" ? "1" : "0.001"}
                          min={spec.min_value ?? undefined}
                          max={spec.max_value ?? undefined}
                          hasError={!!formErrors[key]}
                        />
                      </FieldRow>
                    );
                  })}
                </section>

                {/* Cold params section */}
                {coldParams.length > 0 && (
                  <>
                    <hr className="border-gray-700" />
                    <section className="space-y-4">
                      <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-400">
                        Cold Params
                        <span className="ml-2 text-xs normal-case font-normal text-gray-500">
                          (requires server restart)
                        </span>
                      </h2>
                      {coldParams.map(([key, val]) => (
                        <div key={key} className="flex flex-col gap-1">
                          <span className="text-sm font-medium text-gray-500">{key}</span>
                          <span className="rounded border border-gray-700 px-3 py-1.5 bg-gray-800 text-gray-400 text-sm break-all">
                            {Array.isArray(val) ? (val as unknown[]).join(", ") : String(val)}
                          </span>
                        </div>
                      ))}
                    </section>
                  </>
                )}

                {/* Deploy button + feedback */}
                <div className="flex flex-col gap-3 pt-2">
                  <div className="flex items-center gap-3">
                    <button
                      onClick={handleSubmit}
                      disabled={deployDisabled}
                      className={`rounded px-4 py-2 text-sm font-semibold transition-colors ${
                        deployDisabled
                          ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                          : "bg-indigo-600 text-white hover:bg-indigo-500"
                      }`}
                    >
                      {deployMutation.isPending ? "Deploying…" : "Deploy"}
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
          </>
        )}
      </main>
    </div>
  );
}
