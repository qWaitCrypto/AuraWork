import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND_HOST = process.env.AURA_WEB_HOST || "127.0.0.1";
const BACKEND_PORT = process.env.AURA_WEB_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": `http://${BACKEND_HOST}:${BACKEND_PORT}`,
      "/ws": {
        target: `ws://${BACKEND_HOST}:${BACKEND_PORT}`,
        ws: true,
      },
    },
  },
});

