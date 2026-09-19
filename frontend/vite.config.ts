import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy: /api/* and /health are forwarded to the FastAPI backend so the
// frontend works with zero CORS setup during development. In production set
// VITE_API_BASE to the backend origin instead.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/pipeline': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
