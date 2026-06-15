import { useLiveEvents } from "../lib/useLiveEvents";
import { useXsmomStrategyId } from "../lib/useXsmom";
import { Header } from "../components/Header";
import { XsmomEquityCard } from "../components/xsmom/XsmomEquityCard";
import { XsmomPositions } from "../components/xsmom/XsmomPositions";
import { XsmomScans } from "../components/xsmom/XsmomScans";
import { XsmomRecentEvents } from "../components/xsmom/XsmomRecentEvents";

export default function Xsmom() {
  const strategyId = useXsmomStrategyId();
  const { status } = useLiveEvents(strategyId);
  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="xsmom" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <XsmomEquityCard />
        <XsmomPositions />
        <XsmomScans />
        <XsmomRecentEvents />
      </main>
    </div>
  );
}
