import { useLiveEvents } from "../lib/useLiveEvents";
import { useActiveStrategyId } from "../lib/useActiveStrategyId";
import { Header } from "../components/Header";
import { AlertBanner } from "../components/AlertBanner";
import { ActiveFarbPositions } from "../components/ActiveFarbPositions";
import { EquityCard } from "../components/EquityCard";
import { SignalsStrip } from "../components/SignalsStrip";
import { OpenFarbPositions } from "../components/OpenFarbPositions";
import { RecentEvents } from "../components/RecentEvents";

export { Header } from "../components/Header";

export default function Dashboard() {
  const strategyId = useActiveStrategyId();
  const { status } = useLiveEvents(strategyId);
  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="dashboard" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <AlertBanner />
        <ActiveFarbPositions />
        <EquityCard />
        <SignalsStrip />
        <OpenFarbPositions />
        <RecentEvents />
      </main>
    </div>
  );
}
