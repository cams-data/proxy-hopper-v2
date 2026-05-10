import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Proxy API calls to admin server in dev to avoid CORS
      "/api-proxy": {
        target: process.env.VITE_ADMIN_URL ?? "http://localhost:8081",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api-proxy/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
