import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies API calls to the FastAPI server on :8000.
// `npm run build` writes the app into the Python package, which serves it.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  build: { outDir: "../carequeue/static", emptyOutDir: true },
});
