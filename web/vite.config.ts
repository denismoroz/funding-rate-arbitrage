import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiPort = env.VITE_API_PORT ?? "8000";
  const apiHost = env.VITE_API_HOST ?? "127.0.0.1";
  return {
    plugins: [react()],
    server: {
      allowedHosts: true,
      proxy: {
        "/api": {
          target: `http://${apiHost}:${apiPort}`,
          changeOrigin: true,
        },
        "/ws": {
          target: `ws://${apiHost}:${apiPort}`,
          ws: true,
          changeOrigin: true,
        },
      },
    },
  };
});
