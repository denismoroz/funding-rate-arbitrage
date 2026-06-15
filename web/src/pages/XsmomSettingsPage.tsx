import { useLiveEvents } from "../lib/useLiveEvents";
import { useXsmomStrategyId } from "../lib/useXsmom";
import { Header } from "../components/Header";
import { XsmomSettings } from "../components/xsmom/XsmomSettings";

export default function XsmomSettingsPage() {
  const strategyId = useXsmomStrategyId();
  const { status } = useLiveEvents(strategyId);
  return (
    <div className="min-h-screen bg-gray-50">
      <Header wsStatus={status} route="xsmom-settings" />
      <main className="mx-auto max-w-7xl space-y-4 p-4">
        <XsmomSettings />
      </main>
    </div>
  );
}
