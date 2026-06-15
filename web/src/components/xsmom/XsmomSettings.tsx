import { useState, useEffect } from "react";
import { useXsmomParams, usePatchXsmomParams } from "../../lib/useXsmom";
import { UniverseEditor } from "./UniverseEditor";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";

/** Local form state mirroring the editable params */
type FormState = {
  budget_cap: string;       // string for controlled input
  n_positions_auto: boolean;  // true = let backend decide (null/tercile mode)
  n_positions_value: string;  // "2" | "4" | "6" | ...
  universe: string[];
};

function toFormState(params: Record<string, unknown>, universe: string[]): FormState {
  const nPos = params["n_positions"];
  const isAuto = nPos == null;
  const nPosStr = typeof nPos === "number" ? String(nPos) : "4";
  return {
    budget_cap: typeof params["budget_cap"] === "number" ? String(params["budget_cap"]) : "1000",
    n_positions_auto: isAuto,
    n_positions_value: nPosStr,
    universe,
  };
}

export function XsmomSettings() {
  const { data, isLoading, error } = useXsmomParams();
  const patchMutation = usePatchXsmomParams();

  const [form, setForm] = useState<FormState | null>(null);
  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [clientError, setClientError] = useState<string | null>(null);

  // Initialize form from fetched params
  useEffect(() => {
    if (data) {
      setForm(toFormState(data.params, data.universe));
    }
  }, [data]);

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-sm font-semibold text-gray-700">Settings</h2>
        <Skeleton rows={4} />
      </div>
    );
  }

  if (error instanceof Error) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <h2 className="mb-3 text-sm font-semibold text-gray-700">Settings</h2>
        <ErrorMsg message={error.message} />
      </div>
    );
  }

  if (!form) return null;

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    setClientError(null);
    setSaveNote(null);

    // Client-side validation
    const budget = parseFloat(form.budget_cap);
    if (!isFinite(budget) || budget <= 0) {
      setClientError("Budget must be a positive number.");
      return;
    }
    if (!form.n_positions_auto) {
      const n = parseInt(form.n_positions_value, 10);
      if (!isFinite(n) || n % 2 !== 0 || n <= 0) {
        setClientError("Position count must be a positive even number (2, 4, 6, …).");
        return;
      }
    }
    if (form.universe.length === 0) {
      setClientError("Universe must have at least one coin.");
      return;
    }

    const params: Record<string, unknown> = {
      budget_cap: budget,
      n_positions: form.n_positions_auto ? null : parseInt(form.n_positions_value, 10),
      universe: form.universe,
    };

    patchMutation.mutate({ params }, {
      onSuccess: (result) => {
        setSaveNote(result.restart_required
          ? "Saved. Strategy restart required for changes to take effect."
          : "Saved.");
      },
      onError: (err: Error) => {
        setClientError(err.message);
      },
    });
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-4 text-sm font-semibold text-gray-700">Settings</h2>

      <form onSubmit={handleSave} className="space-y-5">
        {/* Budget cap */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            Budget cap (USDC)
          </label>
          <input
            type="number"
            min={1}
            step={1}
            value={form.budget_cap}
            onChange={(e) => setForm((f) => f ? { ...f, budget_cap: e.target.value } : f)}
            className="rounded border border-gray-300 px-2 py-1 text-sm w-36 focus:outline-none focus:ring-1 focus:ring-indigo-400"
          />
        </div>

        {/* Position count */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            Number of positions
          </label>
          <div className="flex items-center gap-3">
            <label className="inline-flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={form.n_positions_auto}
                onChange={(e) =>
                  setForm((f) => f ? { ...f, n_positions_auto: e.target.checked } : f)
                }
                className="accent-indigo-500"
              />
              Auto (tercile / engine default)
            </label>
            {!form.n_positions_auto && (
              <select
                value={form.n_positions_value}
                onChange={(e) =>
                  setForm((f) => f ? { ...f, n_positions_value: e.target.value } : f)
                }
                className="rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-400"
              >
                {[2, 4, 6, 8, 10].map((n) => (
                  <option key={n} value={String(n)}>
                    {n}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        {/* Universe */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-2">
            Universe (coins)
          </label>
          <UniverseEditor
            universe={form.universe}
            onChange={(next) => setForm((f) => f ? { ...f, universe: next } : f)}
          />
        </div>

        {/* Errors + success */}
        {clientError && (
          <p className="text-xs text-red-600">{clientError}</p>
        )}
        {saveNote && (
          <p className="text-xs text-emerald-600">{saveNote}</p>
        )}
        {patchMutation.isError && !clientError && (
          <ErrorMsg message={(patchMutation.error as Error).message} />
        )}

        <button
          type="submit"
          disabled={patchMutation.isPending}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {patchMutation.isPending ? "Saving…" : "Save"}
        </button>
      </form>
    </div>
  );
}
