import { useState, useEffect } from "react";
import Dashboard from "./pages/Dashboard";
import Settings from "./pages/Settings";

type Route = "dashboard" | "settings";

function readHash(): Route {
  const hash = window.location.hash;
  if (hash === "#/settings") return "settings";
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
  return route === "settings" ? <Settings /> : <Dashboard />;
}
