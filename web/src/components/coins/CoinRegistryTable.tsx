/**
 * CoinRegistryTable — the full coin registry table.
 *
 * Fetches via useCoins() and renders one CoinRow per entry.
 * Columns: coin | leverage | maint_ratio | position_size_usd | active |
 *          spot_token | sz_decimals | bridge_safe | validated | actions
 */

import { useCoins } from "../../lib/useCoins";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";
import { CoinRow } from "./CoinRow";

export function CoinRegistryTable() {
  const { data, isLoading, error } = useCoins();

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
        <Skeleton rows={5} />
      </div>
    );
  }

  if (error instanceof Error) {
    return <ErrorMsg message={error.message} />;
  }

  if (!data || data.length === 0) {
    return (
      <p className="rounded border border-gray-700 bg-gray-800 px-4 py-6 text-center text-sm text-gray-500">
        No coins in registry. Add one below.
      </p>
    );
  }

  return (
    <div className="rounded-lg border border-gray-700 overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="bg-gray-800 text-xs uppercase tracking-wider text-gray-400">
          <tr>
            <th className="px-3 py-2">Coin</th>
            <th className="px-3 py-2">Leverage</th>
            <th className="px-3 py-2">Maint ratio</th>
            <th className="px-3 py-2">Pos. size USD</th>
            <th className="px-3 py-2">Active</th>
            <th className="px-3 py-2">Spot token</th>
            <th className="px-3 py-2 text-right">Sz dec.</th>
            <th className="px-3 py-2">Bridge</th>
            <th className="px-3 py-2">Validated</th>
            <th className="px-3 py-2">Actions</th>
          </tr>
        </thead>
        <tbody className="bg-gray-900 divide-y divide-gray-800">
          {data.map((row) => (
            <CoinRow key={row.coin} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
