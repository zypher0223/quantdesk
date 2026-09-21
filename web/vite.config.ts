import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const GATEWAY = "http://127.0.0.1:8765";
// Long-running engine calls: a reasoning model has been observed taking 145s for
// one research pass, and chart analysis adds a vision round trip. The proxy must
// outlast the slowest call or the browser sees a bare "Failed to fetch".
const LONG_CALL_TIMEOUT = 400_000;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  server: {
    port: 4173,
    proxy: {
      // Everything the local read-only gateway serves, including the
      // engine-computed panels. Without these the browser hits Vite itself
      // and every fetch fails as a 404.
      "/bybit": { target: GATEWAY, timeout: 15000, proxyTimeout: 20000 },
      "/api": { target: GATEWAY, timeout: LONG_CALL_TIMEOUT, proxyTimeout: LONG_CALL_TIMEOUT },
      "/health": { target: GATEWAY, timeout: 5000, proxyTimeout: 8000 },
    },
  },
});
