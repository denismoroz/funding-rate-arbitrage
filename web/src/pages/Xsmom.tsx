import { useLiveEvents } from "../lib/useLiveEvents";
import { useXsmomStrategyId } from "../lib/useXsmom";
import { Header } from "../components/Header";
import { XsmomSummaryCard } from "../components/xsmom/XsmomSummaryCard";
import { XsmomEquityCard } from "../components/xsmom/XsmomEquityCard";
import { XsmomControls } from "../components/xsmom/XsmomControls";
import { XsmomPositions } from "../components/xsmom/XsmomPositions";
import { XsmomScans } from "../components/xsmom/XsmomScans";

export default function Xsmom() {
  const strategyId = useXsmomStrategyId();
  const { status } = useLiveEvents(strategyId);
  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="xsmom" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <XsmomSummaryCard />
        <XsmomEquityCard />
        <XsmomControls />
        <XsmomPositions />
        <XsmomScans />
      </main>
    </div>
  );
}
