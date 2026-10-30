import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Em desenvolvimento (npm run dev) o Vite repassa /api ao backend, como o nginx faz no container.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", rewrite: (caminho) => caminho.replace(/^\/api/, "") },
    },
  },
});
