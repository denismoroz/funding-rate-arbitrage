import { useQuery } from "@tanstack/react-query";
import { fetchEvents } from "../lib/api";
import { formatRelative } from "../lib/format";
import { useNow } from "../lib/useNow";
import { Skeleton } from "./ui/Skeleton";
import { ErrorMsg } from "./ui/ErrorMsg";

const LEVEL_COLOR: Record<string, string> = {
  ERROR: "text-red-600",
  WARNING: "text-yellow-600",
  INFO: "text-blue-600",
  DEBUG: "text-gray-400",
};

export function RecentEvents() {
  const now = useNow();
  const { data, isLoading, error } = useQuery({
    queryKey: ["events"],
    queryFn: () => fetchEvents({ limit: 20 }),
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Recent Events
      </h2>
      {isLoading && <Skeleton rows={4} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-left text-gray-500">
                <th className="pb-1 pr-3">Time</th>
                <th className="pb-1 pr-3">Level</th>
                <th className="pb-1 pr-3">Source</th>
                <th className="pb-1 pr-3">Kind</th>
                <th className="pb-1">Message</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((e, idx) => (
                <tr
                  key={e.id}
                  className={`border-b border-gray-50 hover:bg-gray-100 ${
                    idx % 2 === 0 ? "bg-white" : "bg-gray-50"
                  }`}
                >
                  <td className="py-1 pr-3 text-gray-500">
                    {formatRelative(e.ts_ms, now)}
                  </td>
                  <td
                    className={`py-1 pr-3 font-semibold ${LEVEL_COLOR[e.level] ?? "text-gray-600"}`}
                  >
                    {e.level}
                  </td>
                  <td className="py-1 pr-3 text-gray-600">{e.source}</td>
                  <td className="py-1 pr-3 font-mono text-gray-500">
                    {e.kind}
                  </td>
                  <td className="py-1 text-gray-700">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
