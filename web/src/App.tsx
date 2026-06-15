import { useState, useEffect } from "react";
import Dashboard from "./pages/Dashboard";
import Settings from "./pages/Settings";
import Funding from "./pages/Funding";
import Journal from "./pages/Journal";
import Xsmom from "./pages/Xsmom";

type Route = "dashboard" | "settings" | "funding" | "journal" | "xsmom";

function readHash(): Route {
  const hash = window.location.hash;
  if (hash === "#/settings") return "settings";
  if (hash === "#/funding") return "funding";
  if (hash === "#/journal") return "journal";
  if (hash === "#/xsmom") return "xsmom";
  return "dashboard";
}

function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => readHash());
  useEffect(() => {
    const onChange = () => setRoute(readHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export default function App() {
  const route = useHashRoute();
  if (route === "settings") return <Settings />;
  if (route === "funding") return <Funding />;
  if (route === "journal") return <Journal />;
  if (route === "xsmom") return <Xsmom />;
  return <Dashboard />;
}
