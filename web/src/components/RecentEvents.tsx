import { useState } from "react";
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

const PAGE_SIZE = 20;

export function RecentEvents() {
  const now = useNow();
  const { data, isLoading, error } = useQuery({
    queryKey: ["events"],
    queryFn: () => fetchEvents({ limit: 1000 }),
  });
  const [page, setPage] = useState(0);

  const total = data?.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageEvents = data?.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE) ?? [];

  const pagerBtn =
    "rounded border border-gray-300 bg-gray-50 px-2 py-1 text-xs text-gray-700 hover:bg-gray-100 disabled:opacity-50";

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-3 text-sm font-semibold text-gray-700">
        Recent Events
        {total > 0 && (
          <span className="ml-2 text-[11px] font-normal text-gray-400">
            ({Math.min(PAGE_SIZE, total)} of {total})
          </span>
        )}
      </h2>
      {isLoading && <Skeleton rows={4} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}
      {!isLoading && !error && (
        <>
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
                {pageEvents.map((e, idx) => (
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
          {total > 0 && (
            <div className="mt-3 flex items-center justify-end gap-2">
              <button
                type="button"
                className={pagerBtn}
                disabled={safePage === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Prev
              </button>
              <span className="text-[11px] text-gray-500">
                page {safePage + 1} / {pageCount}
              </span>
              <button
                type="button"
                className={pagerBtn}
                disabled={safePage >= pageCount - 1}
                onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
