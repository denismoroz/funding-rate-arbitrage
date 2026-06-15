import { useState } from "react";

interface UniverseEditorProps {
  universe: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export function UniverseEditor({ universe, onChange, disabled = false }: UniverseEditorProps) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const coin = draft.trim().toUpperCase();
    if (!coin) return;
    if (universe.includes(coin)) {
      setDraft("");
      return;
    }
    onChange([...universe, coin]);
    setDraft("");
  };

  const remove = (coin: string) => {
    onChange(universe.filter((c) => c !== coin));
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {universe.map((coin) => (
          <span
            key={coin}
            className="inline-flex items-center gap-1 rounded-full bg-slate-700 px-2.5 py-0.5 text-xs font-mono font-medium text-slate-100"
          >
            {coin}
            {!disabled && (
              <button
                type="button"
                onClick={() => remove(coin)}
                className="ml-0.5 text-slate-400 hover:text-slate-100"
                aria-label={`Remove ${coin}`}
              >
                ×
              </button>
            )}
          </span>
        ))}
        {universe.length === 0 && (
          <span className="text-xs text-gray-400 italic">No coins</span>
        )}
      </div>

      {!disabled && (
        <div className="flex gap-2">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value.toUpperCase())}
            onKeyDown={handleKeyDown}
            placeholder="Add ticker (e.g. BTC)"
            className="rounded border border-gray-700 bg-gray-900 text-gray-100 px-2 py-1 text-xs font-mono uppercase w-40 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            disabled={disabled}
          />
          <button
            type="button"
            onClick={add}
            disabled={!draft.trim() || disabled}
            className="rounded border border-gray-700 bg-gray-700 px-2 py-1 text-xs text-gray-200 hover:bg-gray-600 disabled:opacity-50"
          >
            Add
          </button>
        </div>
      )}
    </div>
  );
}
