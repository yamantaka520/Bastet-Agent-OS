import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output ships inside the Python package so `bastet serve` can mount it
// at /ui with no Node runtime on the user's machine.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/bastet_agent_os/ui_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8890", ws: true },
    },
  },
});
