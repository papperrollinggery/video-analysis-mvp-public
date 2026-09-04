import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const localBackend = "http://127.0.0.1:8787";
const localProxy = () => ({
  target: localBackend,
  changeOrigin: true,
  headers: { Origin: localBackend }
});

export default defineConfig({
  plugins: [react()],
  server: {
    cors: false,
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": localProxy(),
      "/files": localProxy()
    }
  },
  preview: {
    cors: false
  }
});
