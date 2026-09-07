import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Один порт на всё: прод-сервер раздаёт dist сам; dev-превью проксирует ws на node-сервер.
export default defineConfig({
  root: import.meta.dirname,
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    host: "0.0.0.0",
    port: 5199,
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8790", ws: true },
      "/healthz": { target: "http://127.0.0.1:8790" },
    },
  },
});
