import { useLiveEvents } from "../lib/useLiveEvents";
import { useXsmomStrategyId } from "../lib/useXsmom";
import { Header } from "../components/Header";
import { XsmomSettings } from "../components/xsmom/XsmomSettings";

export default function XsmomSettingsPage() {
  const strategyId = useXsmomStrategyId();
  const { status } = useLiveEvents(strategyId);
  return (
    <div className="min-h-screen bg-gray-900">
      <Header wsStatus={status} route="xsmom-settings" />
      <main className="mx-auto max-w-2xl p-6 space-y-6">
        <h1 className="text-xl font-bold text-white">XSMOM Settings</h1>
        <XsmomSettings />
      </main>
    </div>
  );
}
