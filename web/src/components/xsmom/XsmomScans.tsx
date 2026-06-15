import { useXsmomScans } from "../../lib/useXsmom";
import { type XsmomScanRankItem } from "../../lib/api";
import { formatRelative, formatNumber } from "../../lib/format";
import { useNow } from "../../lib/useNow";
import { Skeleton } from "../ui/Skeleton";
import { ErrorMsg } from "../ui/ErrorMsg";

function RankBadge({ item }: { item: XsmomScanRankItem }) {
  if (item.leg === null) {
    return (
      <span className="text-[11px] font-mono text-gray-400">
        {item.coin} {formatNumber(item.score, 3)}
      </span>
    );
  }
  const cls =
    item.leg === "long"
      ? "bg-indigo-50 text-indigo-600 border border-indigo-200"
      : "bg-rose-50 text-rose-600 border border-rose-200";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-mono ${cls}`}>
      {item.coin} {formatNumber(item.score, 3)} {item.leg}
    </span>
  );
}

function ScanRow({ scan, now }: { scan: import("../../lib/api").XsmomScan; now: number }) {
  // Sort: longs first (score desc), then shorts
  const longs = scan.ranking.filter((r) => r.leg === "long").sort((a, b) => b.score - a.score);
  const shorts = scan.ranking.filter((r) => r.leg === "short").sort((a, b) => b.score - a.score);
  const others = scan.ranking.filter((r) => r.leg === null).slice(0, 5);

  return (
    <div className="border-t border-gray-100 pt-2 pb-1">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-0.5 mb-1">
        <span className="text-xs text-gray-500 shrink-0">
          {formatRelative(scan.ts_ms, now)}
        </span>
        <span className="text-[11px] text-gray-400">
          {scan.n_long}L / {scan.n_short}S
        </span>
        {scan.note && (
          <span className="text-[11px] text-amber-600">{scan.note}</span>
        )}
      </div>
      {(longs.length > 0 || shorts.length > 0) && (
        <div className="flex flex-wrap gap-1 mt-1 overflow-x-auto">
          {longs.map((r) => <RankBadge key={`${r.coin}-l`} item={r} />)}
          {shorts.map((r) => <RankBadge key={`${r.coin}-s`} item={r} />)}
          {others.map((r) => <RankBadge key={`${r.coin}-o`} item={r} />)}
        </div>
      )}
    </div>
  );
}

export function XsmomScans() {
  const now = useNow();
  const { data, isLoading, error } = useXsmomScans(10);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <h2 className="mb-2 text-sm font-semibold text-gray-700">
        Recent Scans
        {data && data.length > 0 && (
          <span className="ml-2 text-[11px] font-normal text-gray-400">
            (latest {data.length})
          </span>
        )}
      </h2>

      {isLoading && <Skeleton rows={3} />}
      {error instanceof Error && <ErrorMsg message={error.message} />}

      {!isLoading && !error && data?.length === 0 && (
        <p className="text-sm text-gray-400">No scans yet</p>
      )}

      {!isLoading && !error && data && data.length > 0 && (
        <div className="space-y-0.5">
          {data.map((scan) => (
            <ScanRow key={scan.id} scan={scan} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}
